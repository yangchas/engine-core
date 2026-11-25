# TD/Rabbit replay row-chunk contract repair — 2026-09-25

## Result

`PASS_WITH_LIMITS`: the t1-v2 replay source no longer exposes or configures a
row-count chunk. Phase P and TASK-008 remain `PARTIAL`; this repair does not
prove production Rabbit delivery membership, arrival order, or live snapshot
visibility.

## Small repair scope

- Removed `ReplayConfig.batch_size` and its `REPLAY_BATCH_SIZE` environment
  mapping. The field only affected `vector::reserve`; it never limited fetched
  or processed rows, but misleadingly suggested row chunking.
- Removed `replay_slice_chunk_no` from `TickBatch`, runtime stats and Redis
  execution context, and removed `chunk_no` from Q2Frame evidence.
- Renamed the source-slice terminal marker to `replay_slice_phase_final` /
  `slice_final_phase`. A fetched slice can have multiple in-memory event-time
  phases only when needed to preserve a business-barrier boundary.
- Kept TD access bounded to one `SELECT` for `[slice_start, slice_end)` at a
  time. All returned rows are retained for that slice; there is no row-count
  cutoff or read-ahead of the full session.

## Evidence

t1-v2 repository commit:

```text
branch: codex/task-q2-pure-function
commit: dde58d64b530f5eec65a34c3254ec6fcb7f930d0
status: local commit only; not pushed, merged or deployed
```

Verification command:

```bash
bash make.sh --full --self-test \
  --out=/home/exedev/validation/t1v2-no-row-chunk-20260925/t1_v2_test
```

Result: full-dependency build and `t1_v2 self-test passed`. Existing hiredis
and TDengine headers emit compiler null-check warnings; no new build failure.

Real, read-only TD slice runs used `--dry-run --q2frame` and the local evidence
sink. The default `TDENGINE_HOST=chaos` did not connect from this host; the
read-only t1-v2 process environment identified `127.0.0.1` and
`market_data1`, after which both bounded reads succeeded. No credential values
were copied into the report.

| TD interval | Input rows | Accepted | Rejected | t1-v2 batches | Q2Frame updates | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-09-23 `[09:25:00,09:25:03)` | 5,071 | 5,071 | 0 | 1 | 5,068 | Full ordinary slice retained |
| 2026-09-23 `[09:24:09,09:24:12)` | 906 | 906 | 0 | 2 event-time phases | 906 | One business-barrier slice retained as one final Q2Frame |

Both artifacts were parsed by Core `Q2FrameV1`; readback passed. SHA-256:

```text
slice-20260923-092500-attempt2.jsonl
6b5a74b8ae9b88b7f8c2d8794ed269c29e5d8f6ecf2a2b212610aaa1f79102a0

barrier-slice-20260923-092409.jsonl
7992b96df58792e32b729b5622be6d154aa08502ec57162b0f0d70c1865fbc09
```

The artifacts are under:
`/home/exedev/validation/t1v2-no-row-chunk-20260925/`.

`td_sql=0`, `ack=0`, and no Redis client was opened (local Q2Frame sink).
No service was restarted and no production file or data was changed.

## Mainline alignment and limits

The ordinary slice confirms the actual TD row set is not capped at 5,000.
The barrier slice deliberately becomes two Engine batch calls after the one
complete TD fetch so the 09:24:10 snapshot occurs between pre- and
post-barrier event-time rows. They are not independent queries or
row-count-sized chunks, and Q2Frame emits one final slice record. Nevertheless,
strict “one Engine call for every 3-second slice” and production Rabbit batch
membership equivalence are not established. Do not erase this distinction by
calling the barrier phases a single Engine call.

```text
ROW_COUNT_CHUNKING=REMOVED
ONE_SELECT_PER_3S_SLICE=PASS_BOUNDED
ALL_ROWS_IN_SLICE_RETAINED=PASS_BOUNDED
ONE_ENGINE_BATCH_PER_SLICE=NOT_TRUE_AT_IN_SLICE_BARRIER
RABBIT_BATCH_EQUIVALENCE=UNPROVEN
REDIS_TD_RABBIT_EFFECTS=NONE
PHASE_P=PARTIAL
TASK_008=PARTIAL_EVIDENCE
```

Resume the existing Phase P plan only after auditing the remaining batch-shape
and barrier semantics. Do not advance the task board, change TASK-008 status,
or start strategy migration based on this repair.
