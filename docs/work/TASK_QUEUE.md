# Task Queue

## READY

### TASK-004

- title: Cross-sectional replay performance closure
- owner: `replay-investigator`
- state: `BLOCKED_BY_PERFORMANCE`
- branch: `codex/task-cross-sectional-performance`
- worktree: current development worktree
- depends_on: `TASK-003`
- goal: complete a 500-frame ordered replay without weakening replay semantics

Merge gate:

- 500 frames and empty frames are retained;
- one trading-day Engine processes 500 market updates;
- ordered `FRAME` performance is measured against the 5/10 minute thresholds;
- incremental state/Q2 results have FULL parity evidence;
- deterministic comparison is run only after the ordered benchmark is viable;
- source sequence, Rabbit arrival, and historical available_at remain UNKNOWN;
- side effects remain `NONE_OBSERVED`.
- result: 500-frame ordered FRAME completed in 835.1 seconds; threshold `>10m`
- evidence: `/home/exedev/validation/replay-20260918-perf-frame-20260920T104925+0800-opt-full`
- FINAL and BOTH passes: `NOT_RUN` after blocking result

### TASK-003

- title: Robust cross-sectional replay foundation
- state: `BLOCKED_BY_PERFORMANCE`
- commit: `2de51f5`
- tests: `623 passed`
- evidence: `/home/exedev/validation/replay-20260918-performance-20260920T101233+0800-final`
- production_side_effects: `NONE_OBSERVED`

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
