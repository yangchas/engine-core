# Task Queue

## REVIEW

### TASK-006

- title: Unified Rabbit-primary canonical tick/batch contract
- owner: `replay-investigator`
- state: `REVIEW`
- branch: `codex/task-cross-sectional-performance`
- worktree: current development worktree
- depends_on: `TASK-005` review and existing C++ RawTick/TickBatch contract
- implementation_commit: `272cbd7428aaf8ac205759ec7d6afa5f230814a8`
- review_metadata_commit: `0fb7fb30260bc04c4026c521b1c9ff0ebf88fafe`
- scope: pure Core contract, TD compatibility adapter, parsed Rabbit fixture,
  canonical hash layers, and legacy shadow projection
- canonical_authority: `RABBITMQ_DATASERVICE_RAWTICK_V1`
- tests: `660 passed`, compileall PASS, diff-check PASS
- protobuf_wire_fixture: PASS after installing `protobuf==4.21.12` in the
  shared verification venv
- production_side_effects: `NONE_OBSERVED`

Merge gate:

- Rabbit `DataRecord/DataBatch → RawTick/TickBatch` remains authoritative;
- TD cannot add fields or redefine Rabbit units/semantics;
- tick parity remains distinct from batch parity;
- source evidence excludes session-local `seq_no`, `wall_ts_ms`, and `run_id`;
- `canonical_content_hash == canonical_semantic_hash`;
- `VALUE_MISMATCH` takes precedence over `ORDER_AMBIGUOUS`;
- trade-date, quality/value, proto3 ambiguity, and symbol normalization tests
  pass;
- no production I/O import, write, ACK, restart, or effect is added;
- integrator review completes before feature-branch merge.

### TASK-004

- title: Cross-sectional replay performance closure
- owner: `replay-investigator`
- state: `REVIEW`
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
- result: ordered FRAME 458.8s (`PASS_WITH_WARN`), ordered FINAL 453.0s
  (`PASS_WITH_WARN`, final FULL parity `PASS`), both FRAME 454.4/456.4s with
  deterministic equality (`PASS_WITH_WARN` per pass)
- evidence:
  - `/home/exedev/validation/replay-20260918-perf-frame-20260920T113146+0800-aggregate-full`
  - `/home/exedev/validation/replay-20260918-perf-final-20260920T114018+0800`
  - `/home/exedev/validation/replay-20260918-perf-determinism-20260920T120503+0800-full`
- frame-mode per-frame FULL parity: `NOT_RUN`; final-mode parity: `PASS`
- tester: `PASS` (`628 passed`, compileall PASS, diff-check PASS)
- ECC production audit: no blocking side-effect or semantic finding; explicit
  integrator acceptance remains required

### TASK-005

- title: Replay session timeline integration
- owner: `replay-investigator`
- state: `REVIEW`
- branch: `codex/task-cross-sectional-performance`
- worktree: current development worktree
- depends_on: `TASK-004`
- implementation: `ReplaySessionTimeline` hash-only ledger
- scope: sequential 3-second frames, 0920/0924/0925 revisions, timer firings,
  and 09:40 checkpoint
- tests: `632 passed`
- production_side_effects: `NONE_OBSERVED`

Merge gate:

- frame manifest is authoritative and EMPTY frames are retained;
- optional auction anchors remain UNKNOWN rather than fabricated;
- late revisions remain versioned and idempotent;
- timer firings are recorded inputs, not scheduled by Core;
- finalization requires every manifest frame;
- no Redis/TD/Rabbit/effect path is added;
- integrator review completes before feature-branch merge.

## BLOCKED

### TASK-003

- title: Robust cross-sectional replay foundation
- state: `BLOCKED_BY_PERFORMANCE`
- commit: `2de51f5`
- tests: `623 passed`
- evidence: `/home/exedev/validation/replay-20260918-performance-20260920T101233+0800-final`
- production_side_effects: `NONE_OBSERVED`

## MERGED

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
