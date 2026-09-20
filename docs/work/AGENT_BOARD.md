# Agent Board

## Columns

`BACKLOG` → `READY` → `RUNNING` → `REVIEW` → `MERGED`<br>
Failure or missing evidence moves a card to `BLOCKED`; obsolete work moves to `ARCHIVED`.

## Current cards

| Card | Owner | State | Branch | Worktree | Merge gate |
|---|---|---|---|---|---|
| TASK-007 Offline canonical replay / auction facts | replay-investigator | RUNNING | `codex/feature-session-engine-integration` | current development worktree | acceptance reopened: propagate degraded-frame evidence, preserve batch quality/order ambiguity/source sequence, join optional session timeline, and advance identical-cohort timing; 676 tests pass; no production source or write path |
| TASK-006 Unified Rabbit-primary canonical tick/batch contract | replay-investigator | MERGED | `codex/feature-session-engine-integration` | current development worktree | canonical tick/batch, TD compatibility adapter, protobuf fixture, hash and legacy shadow gates passed |
| TASK-005 Replay session timeline integration | replay-investigator | MERGED | `codex/feature-session-engine-integration` | current development worktree | hash-only ledger for frames, auction revisions, timer firings, and 09:40 checkpoint |
| TASK-004 Cross-sectional replay performance closure | replay-investigator | MERGED_WITH_WARN | `codex/feature-session-engine-integration` | current development worktree | ordered FRAME/FINAL and both FRAME deterministic evidence; all full passes remain PASS_WITH_WARN |
| TASK-003 Robust cross-sectional replay foundation | replay-investigator | BLOCKED_BY_PERFORMANCE | `codex/feature-session-engine-integration` | integrated into development line | streaming foundation and instrumentation complete; 500-frame replay not verified |
| TASK-001 Real-data replay audit 2026-09-18 09:15-09:40 | replay-investigator | MERGED | `codex/task-real-data-replay-20260918` | `../engine-core-replay-20260918` | read-only bounded replay, deterministic repeat, explicit unknowns, audit recommendation |

Start: `2026-09-20T01:47:07+08:00`; investigator commit: `4828777`; integrated commit: `6404851`.

## Role availability

`planner`, `replay-investigator`, `implementer`, `tester`, and `auditor` are
configured and idle. TASK-001 review completed with tester/auditor PASS.
TASK-004/005/006 integrator review completed with no blocking finding. TASK-007
acceptance was reopened after a read-only audit found evidence propagation and
soft-cutoff timing gaps. No overlapping write is allowed.

## Control-pane rule

TASK-001 was moved to `RUNNING` only after its separate explicit invocation
created the worktree, then moved to `MERGED` only after deterministic replay,
tester, auditor, and repository verification completed.
