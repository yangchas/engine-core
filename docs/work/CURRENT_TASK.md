# Current Task

```text
active_task: TASK-007
bootstrap_status: COMPLETE
last_task: TASK-007
last_task_state: REAL_DATA_PASS_PENDING_INDEPENDENT_REVIEW
next_task: TASK-007 acceptance review
next_task_state: BLOCKED_UNTIL_AUDITOR

owner: replay-investigator
branch: codex/feature-session-engine-integration
worktree: current development worktree
started_at: 2026-09-20T14:10:36+08:00
fix_started_at: 2026-09-20T14:46:36+08:00
implementation_commit: 084d6819b31a80087d624cfabf0d78843c8613ba
latest_fix_commit: aa38614

TASK-004, TASK-005, TASK-006 and TASK-007 passed integrator review on 2026-09-20.
TASK-004 remains `PASS_WITH_WARN` for the 5–10 minute benchmark band; this
does not change the production gate. See
`docs/work/handoffs/INTEGRATOR_REVIEW_20260920.md`.

TASK-007 implementation was merged, then its acceptance was reopened by a
read-only audit. Fixes are limited to the offline canonical replay/auction-facts seam described in
`docs/work/plans/TASK-007-offline-canonical-auction-facts.md`.

The latest fix keeps timing-derived node state in evidence rather than source
semantic identity. Full verification is green (`679 passed`, compileall and
diff-check PASS). Offline tester handoff is PASS. A dedicated real TD ordered
replay has now completed 500 canonical frames (1,224,811 rows, 98 empty frames,
18.07 minutes) with no production side effects. A matching 500-frame shuffled
pass also completed (19.25 minutes), with all comparison hashes equal. The
functional result is PASS;
the performance target is not met and needs optimization, but elapsed time is
not treated as a data or replay correctness failure. A 20-frame real
ordered/shuffled determinism is PASS for the complete 500-frame window. See
`docs/work/handoffs/TASK-007-REAL-DATA-20260920.md`.

TASK-007 remains RUNNING with acceptance pending the independent auditor. The
slow runtime is a follow-up optimization item, not a functional replay
failure; it must not be used to invalidate the real-data evidence.

The next task is not started:

```text
TASK-008: deferred
state: BACKLOG
```
```

TASK-003 established the streamed cross-sectional foundation and performance
instrumentation. TASK-004 completed the incremental identity/Q2 optimization
without weakening the replay contract. The 500-frame ordered FRAME benchmark
processed 1,224,811 events in about 458.8 seconds, and the ordered FINAL
benchmark completed in about 453.0 seconds with final FULL parity PASS. Both
FRAME passes completed in about 454.4/456.4 seconds with deterministic
equality. Each full pass is in the 5–10 minute `PASS_WITH_WARN` band; this is
not a clean <=5 minute performance PASS.

The FRAME runs intentionally report per-frame incremental identity only;
FULL cross-section parity is not claimed for every frame. The FINAL run is the
parity evidence. The shuffled pass changes event order only within each frame;
it is not a Rabbit arrival-order simulation.

The current production gate remains:

```text
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

TASK-004 was accepted with its recorded `PASS_WITH_WARN` performance status.
TASK-005 is merged as a pure in-memory `ReplaySessionTimeline`. TASK-006 is
merged as a RabbitMQ-primary canonical `MarketTickV1`/`TickBatchV1` contract:
Rabbit's parsed `DataRecord/DataBatch → RawTick/TickBatch` shape is authoritative
and TD remains a pure compatibility adapter. No Rabbit/TD/Redis/effect path was
added. The production gate remains independent.
