# Agent Board

## Columns

`BACKLOG` → `READY` → `RUNNING` → `REVIEW` → `MERGED`<br>
Failure or missing evidence moves a card to `BLOCKED`; obsolete work moves to `ARCHIVED`.

## Current cards

| Card | Owner | State | Branch | Worktree | Merge gate |
|---|---|---|---|---|---|
| TASK-001 Real-data replay audit 2026-09-18 09:15-09:40 | replay-investigator | RUNNING | `codex/task-real-data-replay-20260918` | `../engine-core-replay-20260918` | read-only bounded replay, deterministic repeat, explicit unknowns, audit recommendation |

Start: `2026-09-20T01:47:07+08:00`; current commit: `9f7c2a3`.

## Role availability

`planner`, `replay-investigator`, `implementer`, `tester`, and `auditor` are
configured and idle. No child agent is running. No overlapping write is allowed.

## Control-pane rule

TASK-001 was moved to `RUNNING` only after its separate explicit invocation
created the worktree. The input manifest and replay boundary are being frozen
before any engine replay.
