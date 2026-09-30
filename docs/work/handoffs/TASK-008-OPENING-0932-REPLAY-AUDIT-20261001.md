# TASK-008 — 09:32 opening replay evidence audit

Date: 2026-10-01 (Asia/Shanghai)
Result: `REPLAY_FOR_DEVELOPMENT=SUPPORTED_WITH_LIMITS`; task remains
`PARTIAL_EVIDENCE`.

## Scope

Read-only audit of the existing 2026-09-29 exact-release TD→t1-v2→Q2Frame
replay and its Core opening report. The full t1-v2 replay was not repeated.
An independent extraction pass over the frozen Q2Frame recalculated the
per-symbol opening facts with Core's current pure `normalize_q2()` and
`build_open_fact()` functions and compared them with the saved report. This
checks data selection and mapping on the frozen input; because it reuses Core
contracts, it is not an independent market-truth oracle.

## Pinned evidence

- Real TD replay audit:
  `/home/exedev/validation/task008-release-bound-command-replay-20260930T080600+0800/replay_audit.md`
- t1-v2 output consumed by Core:
  `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl`
  SHA-256: `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`
- Saved Core report:
  `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/core_auction_opening_shadow.json`
  SHA-256: `6fdc8485e9877b9f136148b594b1c77cac963868ab5fc27818d68bb2c11f180c`
- t1-v2 release binary SHA-256:
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.

The recorded source window is `[2026-09-29 09:15:00, 09:32:12)` Asia/Shanghai.
The exact-release audit reports 735 t1-v2 batches, 416,870 source rows,
`source_reject=0`, `ack=0`, and `td_sql=0`. Here `td_sql=0` is the TD *write*
counter: the run did read TD. Redis and TD write sinks were disabled; Q2Frame
and auction commands went to local files. `redis_cmds` counts commands sent
to the substituted local sink; `redis_committed_quotes` is the runtime's
logical commit counter after that sink reports success. Neither counter proves
a Redis write.

## 09:32:10 cutoff audit

The Core report consumes 734 Q2Frame records and 418,759 Q2 updates through
the 09:32:10 evaluation second. The next record, 09:32:11 with 774 updates,
is explicitly excluded. At the cutoff:

- 5,223 symbols were observed and 5,223 expected **within the frozen Q2Frame
  cohort**; missing count was 0.
- 12 symbols were stale under the report's 60-second freshness policy;
  5,211 were `READY` and 12 were `PARTIAL`.
- Engine completeness remained `PARTIAL`; availability was
  `UNKNOWN_NOT_INFERRED`; decision status was `FACT_ONLY`.
- `coverage=1.0` is not full-market coverage. The universe basis is
  `UNIQUE_SYMBOLS_IN_FROZEN_Q2FRAME_ONLY`.
- The saved report records ordered/repeat deterministic equality. It does not
  imply Rabbit arrival-order independence.

The local recalculation found the same 5,223-symbol set and zero mismatches
for each of `timestamp_ms`, `change_pct`, `amount_2m_yuan`, `limit_state`,
`speed_1m`, `name`, and `status` against `facts_by_symbol` in the saved Core
report.

## Batch and time limits

The archived Q2Frame is `Q2FrameV1`; its individual records do not carry
`slice_start_ms`, `slice_end_ms`, `source_slice_seq_no`, or delivery metadata.
Its logical timestamps are mostly one second apart because the t1-v2 replay
emitted event-time timestamp groups. The companion exact-release audit
establishes one TD SELECT per configured 3-second half-open slice, but this
Q2Frame artifact alone cannot recover the original Rabbit delivery boundaries
or prove one queue delivery equals one TD slice. Keep these distinct:

```text
TD query slice: 3-second source-read boundary
Q2Frame record: t1-v2 event-time projection group
Rabbit delivery membership/order: UNKNOWN
historical available_at: UNKNOWN
```

The matching 09:29 replay-vs-live auction comparison also remains partial:
the frozen replay and retained live anchor differed on 467 `amount` and 473
`bid_amount` values, plus 480 replay-present/live-null `change_pct` values.
The cause is unknown. This does not invalidate this bounded Core fact
recalculation, but it prevents treating the replay as a general live-parity
oracle.

## Alignment and next step

```text
CORE_0932_FACT_RECALCULATION=PASS_ON_FROZEN_INPUT
REPLAY_FOR_DEVELOPMENT=SUPPORTED_WITH_LIMITS
RABBIT_DELIVERY_MEMBERSHIP=UNKNOWN
RABBIT_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
TASK-008=PARTIAL_EVIDENCE
```

This evidence is useful for iterating on Core opening facts using real
t1-v2-produced values. It does not certify a NORMAL opening or prove live
availability. Do not rerun the same full TD window just to reproduce its
already byte-pinned output; the next useful comparison is a same-date 09:32
live Q2/Core observation when such evidence is available. No task-board or
M3-1/TD-health status is changed by this audit.

## Side effects

The current audit only read frozen local files and ran pure Core functions.
It did not connect to Redis, TD, or Rabbit; write production or validation
data; modify services; or change task state. The earlier source replay used
the documented local-output sinks and disabled Redis/TD writes.
