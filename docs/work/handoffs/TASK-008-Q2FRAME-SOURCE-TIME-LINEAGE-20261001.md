# TASK-008 Q2Frame source-time lineage audit — 2026-10-01

## Result

```text
REAL_Q2FRAME_ROW_REPLAYED_THROUGH_CORE_ADAPTER=PASS
CORE_SOURCE_TIME_PRESERVED=PASS
FRAME_CLOCK_KEPT_SEPARATE=PASS
LEGACY_OPENING_TIMESTAMP_SEMANTICS_DIFFER=CONFIRMED
RABBIT_ARRIVAL_OR_HISTORICAL_AVAILABLE_AT=UNKNOWN
TASK-008=PARTIAL_EVIDENCE
```

This is a bounded real-row lineage check. It changes no Q2 calculation or
production path and does not establish Rabbit arrival time or live visibility.

## Pinned real input and observation

Input Q2Frame:

```text
/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
t1-v2 binary SHA-256: 363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56
```

At source line/sequence 604, the 09:30:00 frame included symbol `000016`:

```text
frame logical_ts_ms: 1790645400000 = 2026-09-29 09:30:00 +08:00
Q2 update ts:        1790644500000 = 2026-09-29 09:15:00 +08:00
difference:          900000 ms
Q2 update SHA-256:   1023aa5e50b4c3f488ab035b614d05c1c2c6d4b5fc6ca63f68e289a8053f8721
```

The update SHA is over UTF-8 JSON with sorted keys and compact separators.
The complete source JSONL hash above pins its original frame and sequence.

## Source path and Core behavior

- `RawTick` processing passes the per-tick `tick.ts_ms` to `QuoteCalculator`;
  `QuoteCalculator::apply_base_tick` stores it in `QuoteState.ts_ms`.
- `Q2ProjectionV1`/`RedisV2Writer` serialize that state timestamp as Q2 `ts`.
- `Q2FrameCommandExecutor` separately serializes frame
  `logical_ts_ms` and the Q2 update fields; the two clocks are not collapsed.
- Core `Q2FrameReplaySource` merges the real update, `normalize_q2` exposes its
  Q2 `ts` as `source_record_time_ms`, and the projection envelope records the
  frame clock as `observed_at_ms`.

The actual Q2Frame was replayed through `Q2FrameReplaySource` up to sequence
604. Core retained `source_record_time_ms=1790644500000`, while the projection
observation time remained `1790645400000`; with a 60-second diagnostic
freshness policy the quote was reported `stale`, not discarded. This is source
event time versus replay-frame observation time, **not** a measured 15-minute
arrival delay.

The pinned engine-next release's `_open_rows_from_frames` instead assigns its
normalized row `timestamp_ms` from `frame.logical_ts_ms`. On this same real
input, that row time is 09:30 while Q2 `ts` is 09:15. Therefore the existing
legacy row's `timestamp_ms` is not interchangeable with Core's per-symbol
`source_record_time_ms`. Do not compare those fields as if they had identical
meaning, and do not change Core to overwrite the source timestamp merely to
force superficial parity.

## Regression and verification

Added a provenance-pinned real-row fixture and a test that verifies:

- the captured Q2 update hash is unchanged;
- source `ts` remains distinct from frame `logical_ts_ms` in Core;
- Core's effective source time and observed frame time retain their respective
  values.

```text
new real-row regression: 1 passed
focused replay/Q2/opening set: 86 passed
full Core suite: 785 passed, 3 upstream protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production reads/writes, Rabbit consume/ACK, service changes: NONE
```

## Alignment and next action

The Core adapter is already doing the right thing for the user's per-symbol
source-time requirement. The concrete migration issue is a legacy field-name
semantic mismatch (`timestamp_ms` means frame logical time in this Q2Frame
normalizer, while Core's per-symbol fact time means Q2 source record time).
Keep both concepts distinct. A later consumer migration should explicitly map
source-record time, frame/evaluation time, and any observed-time concept it
actually needs; `available_at` must remain `UNKNOWN` absent direct evidence.

This check does not prove Rabbit delivery grouping/order, wall-clock arrival,
or historical availability. It neither promotes TASK-008 nor changes
`M3_1_NORMAL=BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`.
