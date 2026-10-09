# TASK-008 C.1 — FRAME_GAP scope robustness

Date: 2026-10-09 (Asia/Shanghai)\
Result: `IMPLEMENTED_AND_REAL_REPLAY_RECHECKED`

## Decision and behavior

The runner inventories slice gaps across the frozen input, but a freshness stop
only applies to the portion relevant to the current replay. The diagnostics
distinguish input inclusion overlap from freshness exposure; a gap after the
replay horizon is recorded and does not abort the run.

- Input inclusion follows `_run_once`: start at the floored replay-window start
  and include logical seconds through `final_barrier_ms` (exclusive end is
  `final_barrier_ms + 1000`). `continue_through_input` extends the horizon to
  the last input logical second.
- If the frame after a gap is consumed, freshness exposure ends at that next
  slice's start. It does not depend on where that frame's tick falls inside the
  slice; this preserves the `57s / 60s / 63s` boundary behavior despite second
  truncation.
- If the next frame is not consumed by this replay, exposure continues to the
  final barrier. Thus a gap that ends before the barrier but whose next frame
  lies after it is not mistaken for recovered input.
- The stop remains strict `exposure > 60,000ms`; equality continues. Source
  sequence gaps are recorded, not treated as temporal gaps. Existing date,
  schema, slice-width, overlap, and raw-time-order checks remain unchanged.
- `freshness_exposure_ms` is a slice-level lower-bound diagnostic, not a
  per-symbol quote-age claim.

The existing Claude Code session was reused for static review. Its review
identified and corrected two possible over-gates: counting through the final
input second instead of the barrier instant, and using a consumed frame's
truncated logical second rather than its slice boundary. The final implementation
follows its recommendation. Static review is not real-data validation.

## Verification

Latest local verification:

```text
pytest: 956 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
```

The synthetic bounded-window tests cover 57/60/63-second gaps with a late
in-slice logical timestamp, a gap whose next frame is beyond the barrier, a
gap after the opening replay horizon, and `continue_through_input`.

The full ordered/repeat replay used frozen local validation artifacts, not live
Redis, TDengine, or RabbitMQ connections:

```text
Q2Frame SHA-256: c6317dcf444ca4b54c36cba43713294ccdf04ba325c1e2abb603c29b67d5a1a4
Barrier sidecar SHA-256: 91daad1c6ec2236796e25b6ce4e5fcf66a077d58c2d1159e3e8e3754b145cc58
Trade date: 2026-09-23
Frames / updates / symbols: 500 / 1,209,672 / 5,222
Ordered and repeat final state hash:
  7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d
Deterministic: true
Final VirtualClock: 2026-09-23 09:39:59 Asia/Shanghai
```

Report:

```text
/home/exedev/validation/task008-frame-gap-final-20261009T074139+0800/core_full_window_report.json
SHA-256: 3c8d2dd1d21badeb354305cdda77f5c08745bb2fd4fc919cc96eec81ae270585
```

The replay process had loaded the runner before the final gap-exposure boundary
refinement. The pinned input contains no slice gaps, so that refinement did not
alter frames or Engine inputs. The final code's `_inventory` was then run over
the same pinned artifact and independently reported: 500/500 slice metadata
complete, zero slice gaps, zero missing slices, zero source-sequence gaps, and
`CONTINUE`. Removing only `.frame_gap_diagnostics` from the new and prior full
reports produced an exact JSON comparison; all existing Engine/replay report
fields and hashes are unchanged.

## Scope and handoff

No production source was queried or written; no service, producer, Redis,
TDengine, RabbitMQ, ACK, or effect path was touched. This work does not change
M3-1 or TD health status.

The current worktree remains dirty with unrelated pre-existing changes. No
files were staged or committed. Do not reset or clean the worktree; review and
commit this runner/test change separately from the existing work.
