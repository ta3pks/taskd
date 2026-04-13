---
project: taskd
version: v2
status: planned
created: 2026-04-14
inputDocuments:
  - architecture decision (memory: decision-taskd-v2-architecture)
  - taskd.py (current source)
stepsCompleted: [requirements]
---

# taskd v2 — Epics and Stories

## Requirements

### Functional Requirements
- FR1: Each claude task must have isolated input/output files (no shared state)
- FR2: Tasks must carry an optional submitter identity
- FR3: Notifications must include task output for claude tasks
- FR4: Purge must clean up per-task bridge directories
- FR5: Existing ani-cli and shell task types must not break
- FR6: State.json must be backward-compatible (new fields default gracefully)

### Non-Functional Requirements
- NFR1: Single Python file (no new dependencies)
- NFR2: Pi-friendly (minimal memory/CPU overhead)
- NFR3: Backward-compatible with existing state.json

---

## Epic 1: Per-Task Isolation for Claude Code Bridge

**Goal:** Eliminate shared bridge files so concurrent claude tasks don't clobber each other.

### Story 1.1: Task-scoped bridge directories
**As** Natasha, **I want** each claude task to have its own input/output directory **so that** multiple claude tasks can run concurrently without data loss.

**Acceptance Criteria:**
- [ ] `_build_claude()` creates `claude-code-bridge/<task-id>/input.md` and writes prompt there
- [ ] Claude output goes to `claude-code-bridge/<task-id>/output.md`
- [ ] `CLAUDE_BRIDGE_DIR` module constant remains as parent dir
- [ ] Old shared `input.md`/`output.md` in bridge root are no longer used
- [ ] Existing non-claude tasks unaffected

**Implementation Notes:**
- Change `_build_claude()` to use `CLAUDE_BRIDGE_DIR / task.id /` instead of `CLAUDE_BRIDGE_DIR /`
- mkdir with `parents=True, exist_ok=True` for the task-scoped dir

### Story 1.2: Notification includes per-task output path
**As** Nikos, **I want** the Telegram notification to include the correct output path **so that** I know where to find results for each task.

**Acceptance Criteria:**
- [ ] Notification for claude tasks references `claude-code-bridge/<task-id>/output.md`
- [ ] Non-claude task notifications unchanged

### Story 1.3: Purge cleans up bridge directories
**As** a system operator, **I want** purge to remove per-task bridge directories **so that** disk space is reclaimed.

**Acceptance Criteria:**
- [ ] When a claude task is purged, its `claude-code-bridge/<task-id>/` directory is deleted
- [ ] Non-claude tasks purge behavior unchanged
- [ ] Missing directories don't cause errors

### Story 1.4: Update SKILL.md with new output path pattern
**As** Natasha, **I want** the skill documentation to reflect per-task output paths **so that** I read results from the correct location.

**Acceptance Criteria:**
- [ ] SKILL.md shows `claude-code-bridge/<task-id>/output.md` pattern
- [ ] Instructions tell Natasha to use taskd show to find the output path

---

## Epic 2: Submitter Field for Multi-Agent Future-Proofing

**Goal:** Track who submitted a task so notification routing can be added later.

### Story 2.1: Add submitter field to Task dataclass
**As** a developer, **I want** tasks to carry an optional submitter identity **so that** we can route notifications per-agent later.

**Acceptance Criteria:**
- [ ] `Task` dataclass has `submitter: str = ""` field
- [ ] Field persists to/from state.json
- [ ] Existing state.json without submitter field loads without error (defaults to "")

### Story 2.2: CLI accepts --submitter flag
**As** Natasha, **I want** to identify myself when submitting tasks **so that** results are attributed correctly.

**Acceptance Criteria:**
- [ ] `taskd add claude --submitter natasha "desc" prompt` works
- [ ] `taskd add shell --submitter natasha "desc" cmd` works
- [ ] Flag is optional, defaults to empty
- [ ] `taskd show <id>` displays submitter if set

### Story 2.3: Notification includes submitter
**As** Nikos, **I want** notifications to show who submitted the task **so that** I know which agent produced the result.

**Acceptance Criteria:**
- [ ] Telegram notification includes submitter name when set
- [ ] Empty submitter = no submitter line in notification

---

## Epic 3: Per-Agent Notification Routing

**Goal:** Route task results to the correct agent/channel based on who submitted the task. Supports multiple ZeroClaw instances (e.g. Natasha for personal, a family agent, a coding agent).

### Story 3.1: Notification routing config
**As** an operator, **I want** to configure per-submitter notification targets **so that** task results reach the right agent.

**Acceptance Criteria:**
- [ ] New env var `TASKD_NOTIFY_ROUTES` accepts JSON mapping: `{"natasha": {"chat_id": "593893526"}, "family": {"chat_id": "FAMILY_CHAT_ID"}}`
- [ ] All routes share the same bot token (`TASKD_TG_BOT_TOKEN`) — one bot, multiple chats
- [ ] Default route (no submitter or unknown submitter) falls back to `TASKD_TG_CHAT_ID`
- [ ] Empty routes config = current behavior (everything to default chat)

### Story 3.2: Route notifications based on submitter
**As** Natasha, **I want** my task results sent to Nikos's DM **so that** family agent results go to the family chat instead.

**Acceptance Criteria:**
- [ ] Task with `submitter=natasha` → routes to natasha's configured chat_id
- [ ] Task with `submitter=family` → routes to family's configured chat_id  
- [ ] Task with no submitter → routes to default `TASKD_TG_CHAT_ID`
- [ ] Unknown submitter → routes to default
- [ ] Routing failure falls back to default silently

### Story 3.3: Per-agent webhook support (optional)
**As** a future agent, **I want** task results delivered via webhook **so that** non-Telegram agents can receive results.

**Acceptance Criteria:**
- [ ] Route config supports optional `webhook_url` per submitter: `{"coding-agent": {"webhook_url": "http://..."}}`
- [ ] If route has `webhook_url`, POST result there instead of Telegram
- [ ] If route has both `chat_id` and `webhook_url`, send to both
- [ ] Webhook failure doesn't block Telegram delivery

---

## Implementation Order

1. Story 1.1 (per-task dirs) — core change, everything else depends on it
2. Story 1.2 (notification path) — small follow-up
3. Story 1.3 (purge cleanup) — keeps disk tidy
4. Story 2.1 (submitter field) — data model change
5. Story 2.2 (CLI flag) — user-facing
6. Story 2.3 (notification submitter) — final touch
7. Story 3.1 (routing config) — depends on submitter field
8. Story 3.2 (route by submitter) — core routing logic
9. Story 3.3 (webhook per agent) — optional, do when needed
10. Story 1.4 (SKILL.md update) — documentation, do last
