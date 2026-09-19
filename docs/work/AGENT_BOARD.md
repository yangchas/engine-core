# Agent Board

## Columns

`BACKLOG` → `READY` → `RUNNING` → `REVIEW` → `MERGED`<br>
Failure or missing evidence moves a card to `BLOCKED`; obsolete work moves to `ARCHIVED`.

## Current cards

| Card | Owner | State | Branch | Worktree | Merge gate |
|---|---|---|---|---|---|
| TASK-001 Real-data replay audit 2026-09-18 09:15-09:40 | replay-investigator | MERGED | `codex/task-real-data-replay-20260918` | `../engine-core-replay-20260918` | read-only bounded replay, deterministic repeat, explicit unknowns, audit recommendation |

Start: `2026-09-20T01:47:07+08:00`; investigator commit: `4828777`; integrated commit: `6404851`.

## Role availability

`planner`, `replay-investigator`, `implementer`, `tester`, and `auditor` are
configured and idle. TASK-001 review completed with tester/auditor PASS. No
overlapping write is allowed.

## Control-pane rule

TASK-001 was moved to `RUNNING` only after its separate explicit invocation
created the worktree, then moved to `MERGED` only after deterministic replay,
tester, auditor, and repository verification completed.
