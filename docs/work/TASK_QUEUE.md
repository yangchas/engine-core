# Task Queue

## RUNNING

### TASK-007

- title: Offline canonical replay / auction facts
- owner: `replay-investigator`
- state: `RUNNING` (acceptance reopened after read-only audit)
- branch: `codex/feature-session-engine-integration`
- worktree: current development worktree
- started_at: `2026-09-20T14:10:36+08:00`
- plan: `docs/work/plans/TASK-007-offline-canonical-auction-facts.md`
- scope: pure Rabbit-primary canonical batch to replay/facts seam
- implementation_commit: `084d6819b31a80087d624cfabf0d78843c8613ba`
- latest_fix_commit: `aa38614` (`state` moved to timing evidence; same auction
  source revision keeps semantic identity)
- handoff: `docs/work/handoffs/TASK-007-offline-canonical-audit.md`
- fix_handoff: `docs/work/handoffs/TASK-007-FIX-20260920.md`
- tester_handoff: `docs/work/handoffs/TASK-007-TESTER-20260920.md`
- integrated_by: `INTEGRATOR_REVIEW_TASK007_20260920.md`
- implementation_tests_before_fix: `668 passed`
- current_tests: `679 passed`, compileall PASS, diff-check PASS
- offline_tester: `PASS`; independent auditor remains pending
- real_data_ordered: `PASS (completed)`; validation:
  `/home/exedev/validation/task007-real-ordered-20260920T154654+0800/`
- real_data_functional: `PASS` (500 canonical frames completed with no side
  effects)
- real_data_performance: `OPTIMIZATION_REQUIRED` (18.07 min for 500 canonical
  frames; not a functional replay failure)
- real_data_determinism: 500-frame ordered/shuffled `PASS`; validation:
  `/home/exedev/validation/task007-real-shuffled-20260920T161558+0800/`
- production_side_effects: `NONE_OBSERVED`

Merge gate:

- canonical batch conversion is deterministic and missing-safe;
- degraded `BLOCKED`/`PARTIAL` frame diagnostics reach Engine evidence;
- batch quality and order ambiguity are not discarded;
- identical auction content advances timing evidence without fabricating a revision;
- no Rabbit/TD/Redis/Wencai/effect import or write path is added;
- ordered/shuffled frame hashes agree;
- empty frames and optional auction anchors remain explicit;
- recovery revision idempotency and late correction evidence pass;
- full pytest, compileall and diff-check pass;
- integrator review is recorded before feature-branch merge.

### TASK-006

- title: Unified Rabbit-primary canonical tick/batch contract
- owner: `replay-investigator`
- state: `MERGED`
- implementation_commit: `272cbd7428aaf8ac205759ec7d6afa5f230814a8`
- integrated_by: `INTEGRATOR_REVIEW_20260920.md`
- branch: `codex/feature-session-engine-integration`
- tests: `660 passed`, compileall PASS, diff-check PASS
- production_side_effects: `NONE_OBSERVED`

### TASK-005

- title: Replay session timeline integration
- owner: `replay-investigator`
- state: `MERGED`
- implementation: `ReplaySessionTimeline` hash-only ledger
- branch: `codex/feature-session-engine-integration`
- tests: `660 passed`, compileall PASS, diff-check PASS
- production_side_effects: `NONE_OBSERVED`

### TASK-004

- title: Cross-sectional replay performance closure
- owner: `replay-investigator`
- state: `MERGED_WITH_WARN`
- branch: `codex/feature-session-engine-integration`
- result: ordered FRAME/FINAL and both FRAME deterministic evidence completed;
  full passes remain in the 5–10 minute `PASS_WITH_WARN` band
- production_side_effects: `NONE_OBSERVED`

## BLOCKED

### TASK-003

- title: Robust cross-sectional replay foundation
- state: `BLOCKED_BY_PERFORMANCE`
- commit: `2de51f5`
- tests: `623 passed`
- evidence: `/home/exedev/validation/replay-20260918-performance-20260920T101233+0800-final`
- production_side_effects: `NONE_OBSERVED`

## MERGED (historical)

### TASK-001

- title: Real-data replay audit for 2026-09-18 09:15-09:40
- owner: `replay-investigator`
- state: `MERGED`
- started_at: `2026-09-20T01:47:07+08:00`
- current_commit: `9f7c2a3`
- branch: `codex/task-real-data-replay-20260918`
- worktree: `../engine-core-replay-20260918`
- execution: bounded read-only audit completed; evidence and review gates passed
- final_status: `REPLAY_READY_BOUNDED`
- validation_dir: `/home/exedev/validation/replay-20260918-0915-0940-20260920T014707+0800`
- tester: `PASS`
- auditor: `PASS`
- current M3-1 status: `BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`

Merge gate:

- replay inputs are read-only and have a manifest;
- bounded replay completes without production side effects;
- two or more runs have identical deterministic hashes;
- event, slice, signal, engine, and final hashes are traceable;
- `UNKNOWN` values, time semantics, availability, and source limits are explicit;
- auditor recommends merge only after tester evidence is complete.

## BACKLOG

All other migration, ownership, strategy, checkpoint, Rabbit, and effect tasks
remain deferred. No card is auto-promoted by this bootstrap.
