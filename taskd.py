#!/usr/bin/env python3
"""
taskd — Lightweight task runner daemon for natasha-pi

Manages long-running tasks (downloads, scripts, etc.) outside of AI session limits.
Runs as a systemd service, persists state to JSON, provides CLI + HTTP API.

Usage:
    taskd start|stop|restart|status    # daemon control
    taskd list [-v]                    # list all tasks
    taskd show <id>                    # detailed task info + progress
    taskd add <type> <name> [args...]  # add a task
    taskd cancel <id>                  # cancel a task
    taskd retry <id>                   # retry a failed task
    taskd log <id> [lines]             # show task log (default 30)
    taskd purge                        # remove completed tasks

Task types:
    ani-cli:  ani-cli anime downloads (args: "Anime Name" -S <n> -e "1-12")
              --dub is ALWAYS added automatically
    claude:   delegate to Claude Code (args: the prompt text)
              e.g. taskd add claude "Weather check" What is the weather in Tbilisi tomorrow?
              Handles bridge files, permissions, and output capture automatically.
    shell:    arbitrary shell command (args: everything after name)

Progress tracking:
    - ani-cli: parses episode range from -e flag, counts completed files + log output
    - shell:   tracks exit code + last log line
    - taskd status: shows progress bars with percentages
    - taskd show <id>: full progress detail + speed info
    - HTTP /task/<id> and /status include progress_pct and progress_detail
"""

import json
import os
import sys
import signal
import subprocess
import time
import threading
import http.server
import socketserver
import shutil
import re
import shlex
import urllib.request
import glob as globmod
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional

# --- Config ---
STATE_DIR = Path(os.environ.get("TASKD_DIR", os.path.expanduser("~/.zeroclaw/taskd")))
STATE_FILE = STATE_DIR / "state.json"
LOG_DIR = STATE_DIR / "logs"
PID_FILE = STATE_DIR / "taskd.pid"
HTTP_PORT = int(os.environ.get("TASKD_PORT", 9100))
POLL_INTERVAL = 5  # seconds between process checks
MAX_CONCURRENT = 2  # max simultaneous tasks
STALL_TIMEOUT = 3600  # seconds of no log output before considering task stalled
NOTIFY_TG_TOKEN = os.environ.get("TASKD_TG_BOT_TOKEN", "")
NOTIFY_TG_CHAT = os.environ.get("TASKD_TG_CHAT_ID", "")
DEFAULT_MEDIA_DIR = Path("/media/media")
CLAUDE_BRIDGE_DIR = Path.home() / ".zeroclaw/workspace/claude-code-bridge"

# Video extensions to count for progress
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".ts"}

# Ensure dirs exist
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_BRIDGE_DIR.mkdir(parents=True, exist_ok=True)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALLED = "stalled"


@dataclass
class Task:
    id: str
    type: str  # "ani-cli", "shell"
    name: str
    args: list = field(default_factory=list)
    status: str = TaskStatus.QUEUED
    pid: Optional[int] = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: Optional[int] = None
    log_file: str = ""
    progress: str = ""
    progress_pct: int = 0  # 0-100
    progress_detail: str = ""  # human-readable: "E08/12 downloading", "rsync 45%"
    total_items: int = 0  # expected items (episodes, files, etc.)
    completed_items: int = 0  # done items
    download_dir: str = ""  # where files go (for file-counting progress)
    priority: int = 0  # higher = first
    retry_count: int = 0
    max_retries: int = 2
    error: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now()
        if not self.log_file:
            self.log_file = str(LOG_DIR / f"{self.id}.log")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short_id():
    import random, string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _parse_episode_range(range_str: str) -> tuple[int, int]:
    """Parse episode range string like '6-12', '1', '1,3,5' into (start, end)"""
    range_str = range_str.strip()
    # Handle "6-12"
    m = re.match(r'^(\d+)\s*-\s*(\d+)$', range_str)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Handle "1,2,3,5"
    if ',' in range_str:
        nums = [int(x.strip()) for x in range_str.split(',')]
        return min(nums), max(nums)
    # Single episode
    try:
        n = int(range_str)
        return n, n
    except ValueError:
        return 0, 0


def _progress_bar(pct: int, width: int = 20) -> str:
    """Return a unicode progress bar string"""
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    filled = int(width * pct / 100)
    empty = width - filled
    return "█" * filled + "░" * empty


class TaskState:
    """Persistent task state manager"""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                for tid, tdict in data.get("tasks", {}).items():
                    self.tasks[tid] = Task(**tdict)
                # Reset any active tasks to stalled (we're starting fresh)
                for t in self.tasks.values():
                    if t.status == TaskStatus.ACTIVE:
                        t.status = TaskStatus.STALLED
                        t.error = "Daemon restarted — process lost"
                self._save()
            except Exception as e:
                print(f"Warning: failed to load state: {e}")

    def _save(self):
        data = {"tasks": {tid: asdict(t) for tid, t in self.tasks.items()}}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(STATE_FILE)

    def add(self, task: Task) -> str:
        with self._lock:
            self.tasks[task.id] = task
            self._save()
        return task.id

    def get(self, tid: str) -> Optional[Task]:
        return self.tasks.get(tid)

    def update(self, task: Task):
        with self._lock:
            self.tasks[task.id] = task
            self._save()

    def list_all(self, status_filter=None) -> list[Task]:
        tasks = list(self.tasks.values())
        if status_filter:
            tasks = [t for t in tasks if t.status == status_filter]
        tasks.sort(key=lambda t: (-t.priority, t.created_at))
        return tasks

    def next_queued(self) -> Optional[Task]:
        """Get highest priority queued task"""
        queued = [t for t in self.tasks.values() if t.status == TaskStatus.QUEUED]
        if queued:
            queued.sort(key=lambda t: (-t.priority, t.created_at))
            return queued[0]
        return None

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == TaskStatus.ACTIVE)

    def purge(self):
        """Remove completed/cancelled/failed tasks older than 1 day"""
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        to_remove = []
        for tid, t in self.tasks.items():
            if t.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
                try:
                    ts = datetime.fromisoformat(t.finished_at).timestamp()
                    if ts < cutoff:
                        to_remove.append(tid)
                except:
                    pass
        for tid in to_remove:
            del self.tasks[tid]
            log = LOG_DIR / f"{tid}.log"
            if log.exists():
                log.unlink()
        if to_remove:
            self._save()
        return len(to_remove)


class TaskRunner:
    """Manages task lifecycle — starts, monitors, kills processes"""

    def __init__(self, state: TaskState):
        self.state = state
        self.processes: dict[str, subprocess.Popen] = {}
        self._log_writers: dict[str, any] = {}

    def start_task(self, task: Task) -> bool:
        if self.state.active_count() >= MAX_CONCURRENT:
            return False

        cmd = self._build_command(task)
        if not cmd:
            task.status = TaskStatus.FAILED
            task.error = f"Unknown task type: {task.type}"
            self.state.update(task)
            return False

        log_path = Path(task.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, 'w')
        self._log_writers[task.id] = log_fh

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes[task.id] = proc
            task.pid = proc.pid
            task.status = TaskStatus.ACTIVE
            task.started_at = _now()
            task.error = ""
            self.state.update(task)
            return True
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            self.state.update(task)
            log_fh.close()
            del self._log_writers[task.id]
            return False

    def _notify(self, task: Task, status: str):
        """Send task result to Telegram and trigger Natasha via webhook"""
        if not NOTIFY_TG_TOKEN or not NOTIFY_TG_CHAT:
            return
        icon = "✅" if status == "completed" else "❌"

        # For claude tasks, send the output directly to Telegram
        if task.type == "claude" and status == "completed":
            output_file = CLAUDE_BRIDGE_DIR / "output.md"
            try:
                result = output_file.read_text().strip()
                tg_msg = f"{icon} *Claude Code: {task.name}*\n\n{result}"
            except Exception:
                tg_msg = f"{icon} taskd: '{task.name}' {status} (couldn't read output)"
        else:
            tg_msg = f"{icon} taskd: '{task.name}' {status}"

        # Send directly via Telegram Bot API
        if NOTIFY_TG_TOKEN and NOTIFY_TG_CHAT:
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{NOTIFY_TG_TOKEN}/sendMessage",
                    data=json.dumps({
                        "chat_id": NOTIFY_TG_CHAT,
                        "text": tg_msg[:4096],
                        "parse_mode": "Markdown",
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass

    def _build_command(self, task: Task) -> Optional[list[str]]:
        if task.type == "ani-cli":
            return self._build_anicli(task)
        elif task.type == "claude":
            return self._build_claude(task)
        elif task.type == "shell":
            return ["bash", "-c", shlex.join(task.args)]
        else:
            return None

    def _build_claude(self, task: Task) -> list[str]:
        """Build Claude Code task. Args are the prompt text (joined with spaces)."""
        prompt = " ".join(task.args) if task.args else task.name
        input_file = CLAUDE_BRIDGE_DIR / "input.md"
        output_file = CLAUDE_BRIDGE_DIR / "output.md"
        input_file.write_text(prompt)
        return [
            "bash", "-c",
            f"claude --dangerously-skip-permissions --allow-dangerously-skip-permissions "
            f"--print -p - < {shlex.quote(str(input_file))} "
            f"> {shlex.quote(str(output_file))} 2>&1"
        ]

    def _build_anicli(self, task: Task) -> list[str]:
        """Build ani-cli download command.

        Task name format: "Chained Soldier S02", "Mashle"
        Task args: [-e "6-12"] [-q best|1080p] [-S <n>] [--query "override search"]

        Season extracted from name (S02 → season 2). Search query from base name.
        Downloads go to /media/media/<ShowName>/S0<N>/
        --dub is ALWAYS added (non-negotiable).
        """
        name = task.name
        args = list(task.args)

        # Extract season from name (e.g. "Chained Soldier S02" → season=2)
        season = 1
        base_name = name
        m = re.search(r'\bS(\d{1,2})\b', name, re.IGNORECASE)
        if m:
            season = int(m.group(1))
            base_name = name[:m.start()].strip()

        # Check for -S in args (overrides name-derived season)
        for i, a in enumerate(args):
            if a == "-S" and i + 1 < len(args):
                season = int(args[i + 1])
                args = args[:i] + args[i+2:]
                break

        # Check for --query override
        search_query = None
        for i, a in enumerate(args):
            if a == "--query" and i + 1 < len(args):
                search_query = args[i + 1]
                args = args[:i] + args[i+2:]
                break
        if not search_query:
            search_query = base_name

        # ALWAYS add --dub (non-negotiable)
        if "--dub" not in args:
            args.append("--dub")

        # Parse episode range for progress tracking
        ep_start, ep_end = 0, 0
        for i, a in enumerate(args):
            if a == "-e" and i + 1 < len(args):
                ep_start, ep_end = _parse_episode_range(args[i + 1])
                break
        if ep_end > 0:
            task.total_items = ep_end - ep_start + 1

        # Build download directory
        season_dir = DEFAULT_MEDIA_DIR / base_name / f"S{season:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)
        task.download_dir = str(season_dir)

        # Build command: ani-cli "query" -d [flags...]
        # Wrap in `script -qc` to provide a pseudo-TTY (ani-cli requires a TTY)
        cmd_str = ' '.join(['ani-cli', f'"{search_query}"', '-d'] + args)
        inner = f'export ANI_CLI_DOWNLOAD_DIR="{season_dir}" && {cmd_str}'
        return ["script", "-qc", inner, "/dev/null"]

    def cancel_task(self, task: Task):
        pid = self.processes.get(task.id)
        if pid:
            try:
                os.killpg(os.getpgid(pid.pid), signal.SIGTERM)
                time.sleep(2)
                if pid.poll() is None:
                    os.killpg(os.getpgid(pid.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            del self.processes[task.id]

        w = self._log_writers.pop(task.id, None)
        if w:
            w.close()

        task.status = TaskStatus.CANCELLED
        task.finished_at = _now()
        self.state.update(task)

    def poll(self):
        """Check all active processes, update status + progress"""
        for tid, proc in list(self.processes.items()):
            task = self.state.get(tid)
            if not task:
                continue

            # Update progress while running
            self._update_progress(task)

            ret = proc.poll()
            if ret is not None:
                # Process finished
                w = self._log_writers.pop(tid, None)
                if w:
                    w.close()

                task.exit_code = ret
                task.finished_at = _now()
                del self.processes[tid]

                # Final progress update
                self._update_progress(task)

                if ret == 0:
                    task.status = TaskStatus.COMPLETED
                    task.progress_pct = 100
                    task.progress_detail = "Done"
                    task.progress = "Done"
                    self._notify(task, "completed")
                else:
                    task.status = TaskStatus.FAILED
                    task.error = f"Exit code {ret}"
                    # Auto-retry
                    if task.retry_count < task.max_retries:
                        task.retry_count += 1
                        task.status = TaskStatus.QUEUED
                        task.error = f"Auto-retry {task.retry_count}/{task.max_retries}"
                    else:
                        self._notify(task, "failed")

                self.state.update(task)

        # Start next queued task
        if self.state.active_count() < MAX_CONCURRENT:
            next_task = self.state.next_queued()
            if next_task:
                self.start_task(next_task)

    def _update_progress(self, task: Task):
        """Update progress for a task based on its type"""
        if task.type == "ani-cli":
            self._update_anicli_progress(task)
        elif task.type == "shell":
            self._update_shell_progress(task)

    def _update_anicli_progress(self, task: Task):
        """Track ani-cli progress via file counting + log parsing"""
        download_dir = Path(task.download_dir) if task.download_dir else None
        log_path = Path(task.log_file)

        # Count completed video files in download dir
        completed = 0
        if download_dir and download_dir.exists():
            for f in download_dir.iterdir():
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    completed += 1

        # Also parse log for current episode being downloaded
        current_ep = None
        downloading = False
        if log_path.exists():
            try:
                content = log_path.read_text()
                lines = content.strip().split('\n')
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    # Look for patterns like "Downloading Episode 8", "EP 8", "E08"
                    m = re.search(r'(?:episode|ep\.?)\s*(\d+)', line, re.IGNORECASE)
                    if m:
                        current_ep = int(m.group(1))
                    if 'download' in line.lower() or 'saving' in line.lower():
                        downloading = True
                    # Check for ffmpeg/aria2 progress percentages
                    m_pct = re.search(r'(\d{1,3})%', line)
                    if m_pct and downloading:
                        task.progress = f"E{current_ep or '?'} — {m_pct.group(1)}%"
                    # Only check last ~10 lines
                    if len([l for l in reversed(lines) if l.strip()][:10]) > 10:
                        break
            except:
                pass

        task.completed_items = completed

        if task.total_items > 0:
            # If we have files already and current episode is being downloaded
            if current_ep and downloading:
                # Current episode is in progress, completed files are already done
                pct = min(99, int((completed / task.total_items) * 100))
                task.progress_pct = pct
                task.progress_detail = f"E{current_ep}/{task.total_items} — {completed} done"
            else:
                pct = min(100, int((completed / task.total_items) * 100))
                task.progress_pct = pct
                task.progress_detail = f"{completed}/{task.total_items} episodes"
        elif completed > 0:
            task.progress_detail = f"{completed} files downloaded"

        # Save last log line as progress
        if log_path.exists() and not task.progress:
            try:
                lines = log_path.read_text().strip().split('\n')
                for line in reversed(lines):
                    line = line.strip()
                    if line and len(line) > 3:
                        task.progress = line[-120:]
                        break
            except:
                pass

    def _update_shell_progress(self, task: Task):
        """Track shell task progress via last log line"""
        log_path = Path(task.log_file)
        if not log_path.exists():
            return
        try:
            lines = log_path.read_text().strip().split('\n')
            for line in reversed(lines):
                line = line.strip()
                if line and len(line) > 3:
                    task.progress = line[-120:]
                    # Try to extract percentage from line
                    m = re.search(r'(\d{1,3})%', line)
                    if m:
                        task.progress_pct = int(m.group(1))
                    break
        except:
            pass

    def get_progress(self, task: Task) -> str:
        """Get progress info string"""
        if task.progress_detail:
            return task.progress_detail
        return task.progress


# --- HTTP API ---

class TaskAPIHandler(http.server.BaseHTTPRequestHandler):
    state: TaskState = None
    runner: TaskRunner = None

    def log_message(self, format, *args):
        pass  # silence HTTP logs

    def _json_response(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def do_GET(self):
        if self.path == "/tasks" or self.path == "/":
            tasks = [asdict(t) for t in self.state.list_all()]
            self._json_response({"tasks": tasks, "total": len(tasks)})

        elif self.path == "/status":
            active = self.state.list_all(TaskStatus.ACTIVE)
            queued = self.state.list_all(TaskStatus.QUEUED)
            stalled = self.state.list_all(TaskStatus.STALLED)
            failed = self.state.list_all(TaskStatus.FAILED)

            # Refresh progress for active tasks
            active_data = []
            for t in active:
                self.runner._update_progress(t)
                active_data.append(asdict(t))

            queued_data = [asdict(t) for t in queued]

            self._json_response({
                "active": len(active),
                "queued": len(queued),
                "stalled": len(stalled),
                "failed": len(failed),
                "max_concurrent": MAX_CONCURRENT,
                "active_tasks": active_data,
                "queued_tasks": queued_data,
            })

        elif self.path.startswith("/task/"):
            tid = self.path.split("/")[-1]
            task = self.state.get(tid)
            if task:
                d = asdict(task)
                if task.status == TaskStatus.ACTIVE:
                    self.runner._update_progress(task)
                    d = asdict(task)
                self._json_response(d)
            else:
                self._json_response({"error": "not found"}, 404)

        elif self.path == "/summary":
            """Quick summary for cron/heartbeat scripts"""
            all_tasks = self.state.list_all()
            summary = {
                "daemon": True,
                "active_count": 0,
                "queued_count": 0,
                "failed_count": 0,
                "stalled_count": 0,
                "active": [],
                "queued": [],
                "failed": [],
                "stalled": [],
            }
            for t in all_tasks:
                if t.status == TaskStatus.ACTIVE:
                    self.runner._update_progress(t)
                    summary["active"].append({
                        "id": t.id,
                        "name": t.name,
                        "type": t.type,
                        "progress_pct": t.progress_pct,
                        "progress_detail": t.progress_detail,
                        "pid": t.pid,
                    })
                elif t.status == TaskStatus.QUEUED:
                    summary["queued"].append({"id": t.id, "name": t.name, "type": t.type})
                elif t.status == TaskStatus.FAILED:
                    summary["failed"].append({"id": t.id, "name": t.name, "error": t.error})
                elif t.status == TaskStatus.STALLED:
                    summary["stalled"].append({"id": t.id, "name": t.name, "error": t.error})
            summary["active_count"] = len(summary["active"])
            summary["queued_count"] = len(summary["queued"])
            summary["failed_count"] = len(summary["failed"])
            summary["stalled_count"] = len(summary["stalled"])
            self._json_response(summary)

        else:
            self._json_response({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        if self.path == "/add":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            task = Task(
                id=_short_id(),
                type=body.get("type", "shell"),
                name=body.get("name", "unnamed"),
                args=body.get("args", []),
                priority=body.get("priority", 0),
            )
            # Auto-set download_dir for ani-cli
            if task.type == "ani-cli":
                cmd = self.runner._build_anicli(task)
                # _build_anicli sets task.download_dir and task.total_items as side effects
            self.state.add(task)
            self._json_response(asdict(task), 201)
        else:
            self._json_response({"error": "unknown endpoint"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/task/"):
            tid = self.path.split("/")[-1]
            task = self.state.get(tid)
            if task:
                if task.status in (TaskStatus.ACTIVE, TaskStatus.STALLED):
                    self.runner.cancel_task(task)
                else:
                    task.status = TaskStatus.CANCELLED
                    self.state.update(task)
                self._json_response({"status": "cancelled"})
            else:
                self._json_response({"error": "not found"}, 404)


# --- Daemon ---

class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True
    allow_reuse_port = True


class TaskDaemon:
    def __init__(self):
        self.state = TaskState()
        self.runner = TaskRunner(self.state)
        self._running = False
        self._http_thread = None
        self._httpd = None
        self._http_retries = 0

        TaskAPIHandler.state = self.state
        TaskAPIHandler.runner = self.runner

    def _start_http_thread(self):
        """Start (or restart) the HTTP API thread"""
        self._http_thread = threading.Thread(target=self._serve_http, daemon=True)
        self._http_thread.start()

    def _handle_signal(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT"""
        self._running = False
        httpd = self._httpd
        if httpd:
            self._httpd = None
            httpd.shutdown()

    def run(self):
        """Main daemon loop"""
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        PID_FILE.write_text(str(os.getpid()))

        self._start_http_thread()

        print(f"taskd started (pid={os.getpid()}, http=:{HTTP_PORT})", flush=True)

        self._recover_stalled()

        # Main loop
        while self._running:
            self.runner.poll()

            # Health check: restart HTTP thread if it died (with circuit breaker)
            if not self._http_thread.is_alive():
                self._http_retries += 1
                if self._http_retries <= 3:
                    print(f"HTTP thread died, restarting (attempt {self._http_retries}/3)...", flush=True)
                    self._start_http_thread()
                elif self._http_retries == 4:
                    print("HTTP thread failed permanently, running without API", flush=True)

            time.sleep(POLL_INTERVAL)

        # Cleanup — signal handler may have already shut down httpd
        httpd = self._httpd
        if httpd:
            self._httpd = None
            httpd.shutdown()
        if PID_FILE.exists():
            PID_FILE.unlink()
        print("taskd shut down cleanly", flush=True)

    def _serve_http(self):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                self._httpd = ReuseTCPServer(("0.0.0.0", HTTP_PORT), TaskAPIHandler)
                with self._httpd:
                    self._httpd.serve_forever()
                    self._http_retries = 0  # reset on clean shutdown
                    return
            except Exception as e:
                print(f"HTTP failed (attempt {attempt+1}/{max_attempts}): {e}", flush=True)
                self._httpd = None
                time.sleep(2 ** attempt)
        print(f"HTTP API failed after {max_attempts} attempts", flush=True)

    def _recover_stalled(self):
        """Check stalled tasks — if their PID is still alive, reactivate"""
        for task in self.state.list_all(TaskStatus.STALLED):
            if task.pid:
                try:
                    os.kill(task.pid, 0)
                    task.status = TaskStatus.ACTIVE
                    self.state.update(task)
                    print(f"Recovered stalled task {task.id} ({task.name}) pid={task.pid}", flush=True)
                except ProcessLookupError:
                    pass


# --- CLI Display ---

STATUS_ICONS = {
    TaskStatus.QUEUED: "🟡",
    TaskStatus.ACTIVE: "🔴",
    TaskStatus.COMPLETED: "✅",
    TaskStatus.FAILED: "❌",
    TaskStatus.CANCELLED: "⛔",
    TaskStatus.STALLED: "⚠️",
}

STATUS_LABELS = {
    TaskStatus.QUEUED: "queued",
    TaskStatus.ACTIVE: "active",
    TaskStatus.COMPLETED: "done",
    TaskStatus.FAILED: "failed",
    TaskStatus.CANCELLED: "cancelled",
    TaskStatus.STALLED: "stalled",
}


def print_task_row(t: Task, show_progress=False):
    """Print a single task row"""
    icon = STATUS_ICONS.get(t.status, "?")
    label = STATUS_LABELS.get(t.status, t.status)

    if show_progress and t.status == TaskStatus.ACTIVE:
        pct = t.progress_pct
        bar = _progress_bar(pct)
        detail = t.progress_detail or t.progress or ""
        print(f"  {icon} {t.id}  {t.name}")
        print(f"     [{bar}] {pct}%  {detail}")
    else:
        extra = ""
        if t.status == TaskStatus.COMPLETED:
            extra = f"  {t.completed_items or '?'} items"
        elif t.status == TaskStatus.FAILED and t.error:
            extra = f"  ({t.error})"
        print(f"  {icon} {t.id}  {t.name}  [{label}]{extra}")


def print_task_detail(t: Task):
    """Print full task detail"""
    icon = STATUS_ICONS.get(t.status, "?")
    label = STATUS_LABELS.get(t.status, t.status)

    print(f"  {icon} {t.id}  {t.name}")
    print(f"  Status:     {label}")
    print(f"  Type:       {t.type}")
    print(f"  Priority:   {t.priority}")
    print(f"  Created:    {t.created_at}")

    if t.started_at:
        print(f"  Started:    {t.started_at}")
    if t.finished_at:
        print(f"  Finished:   {t.finished_at}")
    if t.pid:
        print(f"  PID:        {t.pid}")
    if t.exit_code is not None:
        print(f"  Exit code:  {t.exit_code}")

    # Progress section
    if t.status in (TaskStatus.ACTIVE, TaskStatus.COMPLETED, TaskStatus.FAILED):
        pct = t.progress_pct
        bar = _progress_bar(pct)
        print(f"  Progress:   [{bar}] {pct}%")
        if t.progress_detail:
            print(f"  Detail:     {t.progress_detail}")
        if t.total_items:
            print(f"  Items:      {t.completed_items}/{t.total_items}")
        if t.progress and t.progress != t.progress_detail:
            print(f"  Last line:  {t.progress}")

    if t.download_dir:
        print(f"  Download:   {t.download_dir}")
    if t.error:
        print(f"  Error:      {t.error}")
    if t.retry_count:
        print(f"  Retries:    {t.retry_count}/{t.max_retries}")
    if t.args:
        print(f"  Args:       {' '.join(t.args)}")


def print_status(data: dict):
    """Print status response from API"""
    print(f"taskd — Active: {data['active']}/{data['max_concurrent']}  Queued: {data['queued']}  Stalled: {data.get('stalled', 0)}  Failed: {data.get('failed', 0)}")

    if data.get("active_tasks"):
        print("\n🔴 Active:")
        for t in data["active_tasks"]:
            pct = t.get("progress_pct", 0)
            bar = _progress_bar(pct)
            detail = t.get("progress_detail", "") or t.get("progress", "")
            pid = t.get("pid", "?")
            print(f"   {t['id']}  {t['name']}  (pid={pid})")
            if pct or detail:
                print(f"     [{bar}] {pct}%  {detail}")

    if data.get("queued_tasks"):
        print("\n🟡 Queued:")
        for t in data["queued_tasks"]:
            print(f"   {t['id']}  {t['name']}  priority={t.get('priority', 0)}")

    if data.get("failed") and data["failed"] > 0:
        # Fetch failed tasks from full list
        pass  # brief status doesn't include failed details


def query_api(endpoint):
    """Query the running daemon via HTTP"""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{HTTP_PORT}{endpoint}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"Daemon not responding: {e}")
        print("Start it first: taskd start")
        return None


def _stop_daemon():
    """Send SIGTERM and wait for the daemon to exit. Returns True if it was running."""
    if not PID_FILE.exists():
        return False
    pid = int(PID_FILE.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
    except ProcessLookupError:
        pass
    if PID_FILE.exists():
        PID_FILE.unlink()
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "start":
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text())
            try:
                os.kill(pid, 0)
                print(f"Already running (pid={pid})")
                sys.exit(0)
            except ProcessLookupError:
                PID_FILE.unlink()

        daemon = TaskDaemon()
        daemon.run()

    elif cmd == "stop":
        if _stop_daemon():
            print("Stopped.")
        else:
            print("Not running.")

    elif cmd == "restart":
        if os.environ.get("INVOCATION_ID"):
            print("Use: systemctl restart taskd")
            sys.exit(1)

        ret = subprocess.run(
            ["systemctl", "is-active", "--quiet", "taskd"],
            capture_output=True,
        )
        if ret.returncode == 0:
            subprocess.run(["sudo", "systemctl", "restart", "taskd"], check=False)
            print("Restarted (via systemd).")
        else:
            _stop_daemon()
            subprocess.Popen(
                [sys.executable, sys.argv[0], "start"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("Restarted.")

    elif cmd == "status":
        data = query_api("/status")
        if data:
            print_status(data)

    elif cmd == "list":
        data = query_api("/tasks")
        if data:
            tasks = data.get("tasks", [])
            if not tasks:
                print("No tasks.")
                return

            # Group by status
            groups = {}
            for t in tasks:
                s = t["status"]
                if s not in groups:
                    groups[s] = []
                groups[s].append(t)

            # Order: active, stalled, queued, failed, completed, cancelled
            order = ["active", "stalled", "queued", "failed", "completed", "cancelled"]
            for status in order:
                if status not in groups:
                    continue
                label = STATUS_LABELS.get(status, status)
                icon = STATUS_ICONS.get(status, "?")
                print(f"\n{icon} {label.upper()} ({len(groups[status])}):")
                for t in groups[status]:
                    task_obj = Task(**t)
                    print_task_row(task_obj, show_progress=(status == "active"))

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: taskd show <id>")
            sys.exit(1)
        tid = sys.argv[2]
        data = query_api(f"/task/{tid}")
        if data:
            task = Task(**data)
            print_task_detail(task)
        else:
            print(f"Task {tid} not found")

    elif cmd == "summary":
        """Machine-readable summary (for scripts/cron)"""
        data = query_api("/summary")
        if data:
            print(json.dumps(data, indent=2))

    elif cmd == "log":
        if len(sys.argv) < 3:
            print("Usage: taskd log <id> [lines]")
            sys.exit(1)
        tid = sys.argv[2]
        lines_count = 30
        if len(sys.argv) >= 4:
            try:
                lines_count = int(sys.argv[3])
            except ValueError:
                pass
        log_path = LOG_DIR / f"{tid}.log"
        if log_path.exists():
            lines = log_path.read_text().strip().split('\n')
            for line in lines[-lines_count:]:
                print(line)
        else:
            print(f"No log for task {tid}")

    elif cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: taskd add <type> <name> [args...]")
            print("  type: ani-cli | shell")
            print("  Note: --dub is ALWAYS added for ani-cli tasks")
            print("  Examples:")
            print('    taskd add ani-cli "Chained Soldier S2" "Chained Soldier" -S 1 -e "6-12"')
            print('    taskd add shell "Sync Obsidian" rsync -av ~/todos/ /backup/todos/')
            print('    taskd add ani-cli "Frieren S2" "Frieren" -S 2 -e "1-28" -p 5')
            sys.exit(1)

        task_type = sys.argv[2]
        name = sys.argv[3]
        args = sys.argv[4:]

        # Priority from -p flag
        priority = 0
        if "-p" in args:
            idx = args.index("-p")
            if idx + 1 < len(args):
                priority = int(args[idx + 1])
                args = args[:idx] + args[idx+2:]

        # For ani-cli, pre-parse to set total_items
        task = Task(
            id=_short_id(),
            type=task_type,
            name=name,
            args=args,
            priority=priority,
        )

        # If daemon is running, send via HTTP (daemon will handle _build_anicli side effects)
        import urllib.request
        try:
            url = f"http://127.0.0.1:{HTTP_PORT}/add"
            body = json.dumps({"type": task_type, "name": name, "args": args, "priority": priority}).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                result = json.loads(resp.read())
                pct_info = ""
                if result.get("total_items"):
                    pct_info = f"  ({result['total_items']} episodes)"
                print(f"Added task: {result['id']}  {name}  [{result['status']}]{pct_info}")
        except:
            # Daemon not running — add directly to state
            state = TaskState()
            # Run _build_anicli to set download_dir and total_items
            runner = TaskRunner(state)
            runner._build_anicli(task)
            state.add(task)
            pct_info = ""
            if task.total_items:
                pct_info = f"  ({task.total_items} episodes)"
            print(f"Added task (offline): {task.id}  {name}  [{task.status}]{pct_info}")
            print("Note: daemon not running. Start with: taskd start")

    elif cmd == "cancel":
        if len(sys.argv) < 3:
            print("Usage: taskd cancel <id>")
            sys.exit(1)
        tid = sys.argv[2]
        data = query_api(f"/task/{tid}")
        if data and data.get("status") in ("active", "stalled", "queued"):
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/task/{tid}", method="DELETE")
            urllib.request.urlopen(req, timeout=3)
            print(f"Cancelled task {tid}")
        elif data:
            print(f"Task {tid} is {data.get('status')} — can't cancel")
        else:
            print(f"Task {tid} not found")

    elif cmd == "retry":
        if len(sys.argv) < 3:
            print("Usage: taskd retry <id>")
            sys.exit(1)
        tid = sys.argv[2]
        data = query_api(f"/task/{tid}")
        if data and data.get("status") == "failed":
            # Update via state directly since no retry endpoint
            state = TaskState()
            task = state.get(tid)
            if task:
                task.status = TaskStatus.QUEUED
                task.error = ""
                task.progress_pct = 0
                state.update(task)
                print(f"Task {tid} re-queued")
            else:
                print(f"Task {tid} not found in state")
        elif data:
            print(f"Task {tid} is {data.get('status')} — not failed")
        else:
            print(f"Task {tid} not found")

    elif cmd == "purge":
        state = TaskState()
        count = state.purge()
        print(f"Purged {count} old tasks")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
