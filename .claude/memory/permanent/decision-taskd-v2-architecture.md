---
id: decision-taskd-v2-architecture
title: taskd v2 architecture
type: decision
scope: project
created: "2026-04-13T21:56:38.333Z"
updated: "2026-04-13T21:56:38.333Z"
tags:
  - promoted-from-think
  - project
---

# taskd v2 architecture

## Architecture Decision: Incremental, Not Big-Bang

**Do now (v2):**
1. Per-task directories for claude bridge files (task-id scoped)
2. Add submitter field to Task (future-proofing, cheap)
3. Keep Telegram Bot API notifications (working, direct)
4. Purge cleans up per-task bridge dirs

**Do when needed (v3+):**
- Multi-channel notification routing (when second channel exists)
- Agent-specific notification endpoints
- Natasha conversation injection (if polling delay becomes unacceptable)

**Don't do:**
- Abstract notification interfaces
- Message queues or event buses
- Webhook-based conversation injection (tried, doesn't work with ZeroClaw's architecture)

The current design is 90% right for a personal assistant. Per-task isolation is the only real bug. Everything else is premature.

_Deliberation: `thought-20260414-015518894`_
