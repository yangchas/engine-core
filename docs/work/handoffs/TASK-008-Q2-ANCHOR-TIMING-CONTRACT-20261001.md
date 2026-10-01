# TASK-008 Q2 anchor timing contract correction — 2026-10-01

## Finding

The local `Q2FieldSpec` edit incorrectly named `09:20:03` / `09:24:10` /
`09:25:06` as each Q2 field's `first_observable_time`. Those are separate
snapshot-trigger thresholds. In the exact-release producer, `a20`/`a24`/`a25`
are populated by qualifying source-event ticks and included in ordinary Q2
updates before those thresholds.

## Evidence

Read-only source inspection of release `20260923_tdstop0945b` confirms:

- source timestamps are truncated to whole local seconds before the anchor
  event-time windows are checked;
- each qualifying tick overwrites the per-symbol candidate in processing
  order; there is no maximum-source-time guard in the anchor update;
- the scheduled snapshot trigger is a separate threshold at 09:20:03,
  09:24:10, and 09:25:06;
- normal Q2 writes occur after batch processing, and anchor-dirty states are
  eligible for Q2 projection before that snapshot trigger.

The pinned 2026-09-29 exact-release Q2Frame also has nonzero candidates in the
first corresponding whole-second event-time frame, before each threshold:

| Field | First observed frame | Nonzero symbols | Example |
| --- | --- | ---: | --- |
| `a20` | 09:20:00, seq 301 | 626 | `000001=11300` milli-price |
| `a24` | 09:24:00, seq 541 | 307 | `600000=9130` milli-price |
| `a25` | 09:25:00, seq 601 | 3,525 | `000001=11300` milli-price |

Input: `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl`, SHA-256
`5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`.
Machine-readable evidence and exact producer source hashes:
`/home/exedev/validation/task008-q2-anchor-timing-20261001T081955+0800/anchor_timing_audit.json`.

## Bounded correction and verification

`Q2FieldSpec` now separates:

- the qualifying source-event window;
- `candidate_update_order=LAST_PROCESSED_QUALIFYING_TICK`;
- `snapshot_trigger_not_before`, which describes the separate scheduled
  barrier and is explicitly not a field-availability timestamp.

No calculation, trigger, source-selection, freshness, strategy, Redis/TD/Rabbit
path, or production behavior changed. No live data source was accessed.

```text
targeted Q2 tests: 33 passed
full pytest: 760 passed, 3 protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
TASK-008: PARTIAL_EVIDENCE (unchanged)
```

## Limits and alignment

This proves pre-trigger candidate presence in one pinned replay artifact. It
does not prove historical Redis `available_at`, live visibility, Rabbit
delivery membership/order, or wall-clock observability. Do not label the
barrier time as candidate availability, and do not use event-time replay order
as a proxy for production processing order. TASK-008 remains partial; this
finding does not promote a new phase or add a production gate.
