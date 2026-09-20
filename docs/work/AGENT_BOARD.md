# Agent Board

## Columns

`BACKLOG` → `READY` → `RUNNING` → `REVIEW` → `MERGED`<br>
Failure or missing evidence moves a card to `BLOCKED`; obsolete work moves to `ARCHIVED`.

## Current cards

| Card | Owner | State | Branch | Worktree | Merge gate |
|---|---|---|---|---|---|
| TASK-004 Cross-sectional replay performance closure | replay-investigator | REVIEW | `codex/task-cross-sectional-performance` | current development worktree | ordered FRAME 458.8s, FINAL 453.0s with final parity PASS, both FRAME deterministic; all full passes are PASS_WITH_WARN |
| TASK-003 Robust cross-sectional replay foundation | replay-investigator | BLOCKED_BY_PERFORMANCE | `codex/feature-session-engine-integration` | integrated into development line | streaming foundation and instrumentation complete; 500-frame replay not verified |
| TASK-001 Real-data replay audit 2026-09-18 09:15-09:40 | replay-investigator | MERGED | `codex/task-real-data-replay-20260918` | `../engine-core-replay-20260918` | read-only bounded replay, deterministic repeat, explicit unknowns, audit recommendation |

Start: `2026-09-20T01:47:07+08:00`; investigator commit: `4828777`; integrated commit: `6404851`.

## Role availability

`planner`, `replay-investigator`, `implementer`, `tester`, and `auditor` are
configured and idle. TASK-001 review completed with tester/auditor PASS. TASK-004
has tester PASS and ECC audit with no blocking finding; integrator acceptance is
still pending. No overlapping write is allowed.

## Control-pane rule

TASK-001 was moved to `RUNNING` only after its separate explicit invocation
created the worktree, then moved to `MERGED` only after deterministic replay,
tester, auditor, and repository verification completed.
