# Phase P auction-barrier trace audit — 2026-09-25

Status: `BOUNDED_REAL_REPLAY_PARITY_PASS / PHASE_P_PARTIAL`

## Scope and boundary

The t1-v2 development replay was instrumented to capture all active per-symbol
quote/auction state at the 09:20, 09:24 and 09:25 barriers. The run read real
TD rows for trade date `2026-09-23`, `[09:15:00,09:25:09)`, one SELECT per
3-second half-open slice, and wrote only to Redis DB15 under the unique prefix
`phasepdiag20260925T0840:`. TD writes, Rabbit consume/ACK, service changes and
production Redis DB0 writes were not performed.

## Result

```text
REAL_TD_3S_SLICED_REPLAY              PASS
BARRIER_TRACE_0920_0924_0925          PASS
Q2_5222_HASHES_VS_DB5_BASELINE        PASS
Q2_ACTIVE_SET_VS_DB5_BASELINE         PASS
A2_AND_LEGACY_0920_0924_0925          PASS
0925_ANCHOR_VS_DB5_AND_CONTROL        PASS
NO_FUTURE_SOURCE_TIMESTAMP_AT_BARRIER PASS
PRODUCTION_SIDE_EFFECTS               NONE_OBSERVED
PHASE_P                              PARTIAL
```

Run summary: `batches=204`, `clocks=1` at `09:25:06`, `source_in/ticks=212022`,
`source_reject=0`, `redis_cmds=425502`, `redis_committed=211932`, `td_sql=0`,
`ack=0`. The trace contains 5,222 member rows at each barrier. Candidate
counts are 4,873 at 09:20:03 (tick trigger), 5,100 at 09:24:10 (tick trigger),
and 5,208 at 09:25:06 (Clock trigger). All per-symbol quote and auction source
timestamps are at or before their barrier. For 09:25:06, the latest observed
source timestamp is 09:25:03; no later TD event was pulled into the frozen
state. The 09:25 trace candidate-symbol set equals the 5,208 members in the
Redis anchor archive.

## Redis comparison

Read-only comparison against DB5/`task009k:` found 5,222/5,222 Q2 symbol
hashes equal, with an equal 5,222-member active set. A2 (four fields) and
legacy auction projections (three fields) match exactly for 0920, 0924 and
0925; their `meta.n` values are respectively 4,873, 5,100 and 5,208. The
09:25 anchor is byte-equal to DB5 and the prior source-aligned DB15 control;
length 539,474 bytes, SHA-256
`1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb`.

The same A2, legacy and anchor values also match the source-aligned DB15
control `task009pbarrierinc20260925T042334:`. That control uses custom keys
`legacy-auction:<date>:<tag>` and `anchor:<date>`, unlike the default/current
`market:auction:<date>:<tag>` and `market:auction:anchor:<date>`. An earlier
comparison attempt assumed the latter key names for the control; its apparent
missing-key result was an audit-script key-model error, not missing replay
output. The 0920/0924 full symbol membership is captured in this new trace;
the old anchor archive only stores a full member map for 0925.

The candidate DB15 namespace contains 5,233 keys; the same prefix has zero
matches in production DB0. The trace CSV SHA-256 is
`9c7c17bab9853d8245875d0edef5623d95dab75f708a7b014fe699931567feb6`.

## Implementation and verification

- t1-v2 branch: `codex/task-q2-pure-function`.
- Local commit: `5c61f432360b4cc604882289a12b5501f7913458`
  (`feat(replay): capture auction barrier state trace`); not pushed, merged or
  deployed. The repository has no Git remote configured and is clean after
  commit.
- The opt-in `REPLAY_BARRIER_AUDIT_PATH` trace is rejected outside Replay mode.
  The C++ self-test checks captured source timestamps and verifies Live mode
  does not create the trace file.
- Full dependency build plus self-test passed at
  `/home/exedev/validation/t1v2-phasep-audit-20260925T084000+0800/t1v2-audit-green-final`.
  Existing hiredis/TDengine array-address compiler warnings remain unrelated.
- The real-data replay used `t1v2-audit-green`, built with the same runtime
  instrumentation before a final self-test-only temporary-path isolation
  tweak; runtime behavior is identical to the committed source. Its SHA-256 is
  `97bb9928cebd0619a485d69f0a308d126ed3fb3c75b52e8eb5fc85a0a15ca16c`.
- After verification, `t1-v2-live` and `engine-next` were `active`, both with
  `NRestarts=0`; `/` and `/home/exedev` had 22 GB available. No service command
  changed either service.

## Limits and next alignment

This bounded result corrects the earlier claim that the post-`ca5ece0`
development replay remained different from the DB5 auction/Q2 baseline: the
observed Redis outputs for this exact date and window are now equal. It does
not prove deployed-binary identity, all-date equivalence, Rabbit DataBatch
membership or arrival order, completion watermark, or historical
`available_at`. TD same-timestamp/symbol source ordering remains
`UNSPECIFIED/UNKNOWN`; the replay trace must not be read as Rabbit arrival
evidence. This does not complete Phase P or TASK-008, and it does not change
`M3_1_NORMAL=BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`. No next phase is auto-started.
