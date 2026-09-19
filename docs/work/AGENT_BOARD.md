# Agent Board

## Columns

`BACKLOG` → `READY` → `RUNNING` → `REVIEW` → `MERGED`<br>
Failure or missing evidence moves a card to `BLOCKED`; obsolete work moves to `ARCHIVED`.

## Current cards

| Card | Owner | State | Branch | Worktree | Merge gate |
|---|---|---|---|---|---|
| TASK-001 Real-data replay audit 2026-09-18 09:15-09:40 | replay-investigator | READY | `codex/task-real-data-replay-20260918` | `../engine-core-replay-20260918` (not created) | read-only bounded replay, deterministic repeat, explicit unknowns, audit recommendation |

## Role availability

`planner`, `replay-investigator`, `implementer`, `tester`, and `auditor` are
configured and idle. No child agent is running. No overlapping write is allowed.

## Control-pane rule

Do not move TASK-001 to `RUNNING` until a separate explicit invocation creates
its worktree, records the input manifest, and confirms the replay boundary.
