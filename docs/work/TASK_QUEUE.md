# Task Queue

## REVIEW

### TASK-007

- title: Offline canonical replay / auction facts
- owner: `replay-investigator`
- state: `REVIEW`
- branch: `codex/task-offline-canonical-auction-facts`
- worktree: current development worktree
- started_at: `2026-09-20T14:10:36+08:00`
- plan: `docs/work/plans/TASK-007-offline-canonical-auction-facts.md`
- scope: pure Rabbit-primary canonical batch to replay/facts seam
- implementation_commit: `43a3dfc99c191fc2a3998879e7de26da51b1a2f5`
- handoff: `docs/work/handoffs/TASK-007-offline-canonical-audit.md`
- tests: `668 passed`, compileall PASS, diff-check PASS
- production_side_effects: `NONE_OBSERVED`

Merge gate:

- canonical batch conversion is deterministic and missing-safe;
- no Rabbit/TD/Redis/Wencai/effect import or write path is added;
- ordered/shuffled frame hashes agree;
- empty frames and optional auction anchors remain explicit;
- recovery revision idempotency and late correction evidence pass;
- full pytest, compileall and diff-check pass;
- integrator review completes before feature-branch merge.

## MERGED

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
