# TD one-batch-per-slice repair audit — 2026-09-25

## Scope and outcome

This bounded repair closes the current-source behavior where a single TD
three-second SELECT could be emitted as multiple t1-v2 batches solely to place
a business barrier. Each TD half-open slice now becomes exactly one
`TickBatch`, with all converted rows retained. A barrier is an in-batch
event-time cut: t1-v2 processes the prefix, freezes/emits the barrier output,
then continues with the remaining rows in the same `EngineCore::on_batch`
call. It does not re-query TD, split rows into source batches, or impose a row
chunk limit.

This is a deterministic event-time reconstruction contract. It is not proof
that a TD slice has the same members or boundary as a Rabbit delivery batch.
Rabbit delivery membership/order and historical `available_at` remain
`UNKNOWN`.

## Code and local commits

- t1-v2 branch `codex/task-q2-pure-function`, commit
  `26f60ae0c87d109421b71ce4dae6f4c8e5025e2e`:
  one returned `TickBatch` per SELECT, in-batch barrier cuts, per-tick phase
  resolution for replay slices, and exact in-cut audit snapshots including
  explicit `0926` auction-close labeling.
- Core branch `codex/feature-session-engine-integration`, commit
  `5cc45ac4976821ceebf839234c3f9439318ce9f7`:
  accepts empty Q2Frame timeline records without inventing an expected symbol
  universe; reports coverage as unknown when that denominator is unavailable.
- Both commits are local only. No push, merge, or deployment was performed.

## Real-source evidence

Only TD reads were issued. All output went to the validation directory below;
the binary used local Q2Frame output, with Redis and TDengine writes disabled.

1. `2026-09-23 [09:24:09,09:24:12)`:
   one TD slice returned 906 rows. The 09:24:10 in-slice barrier audit
   contained 603 then-observed auction states. The whole slice remained one
   final Q2Frame.
2. `2026-09-23 [09:25:06,09:25:09)`:
   TD returned zero rows. t1-v2 still emitted one empty Q2Frame and fired the
   09:25:06 barrier with zero quote states. Core replayed that real frame
   twice, advancing one time frame/reducer revision deterministically while
   reporting `UNKNOWN_NO_UNIVERSE`; this proves timeline handling only, not
   market-data coverage.
3. `2026-09-23 [09:26:00,09:26:03)`:
   the final validation binary issued one bounded TD query and returned 5
   ticks in one Q2Frame (`symbol_count=5`, exact three-second slice bounds).
   Audit output has one `summary,0926,...,tick_batch_barrier` row and 5 member
   rows/states, matching the five source rows at the close second. No `0925`
   fallback label was emitted for this close barrier.

Evidence paths:

- Main validation directory:
  `/home/exedev/validation/t1v2-one-batch-repair-20260925/`
- Final 09:26 Q2Frame and barrier audit:
  `/home/exedev/validation/t1v2-one-batch-repair-20260925/real-0926-audit-fixed-20260925/`
- Core empty-frame replay report:
  `/home/exedev/validation/t1v2-one-batch-repair-20260925/core-empty-frame-replay-20260925T103857+0800/replay-report.json`

The TD query was read-only. The `td_sql=0` runtime counter denotes zero TD
write statements; it does not mean no SELECT was executed.

## Verification

- t1-v2 full-dependency build plus built-in self-test: PASS (`t1_v2 self-test passed`).
- Core full test suite: PASS (`693 passed`; 3 dependency deprecation warnings).
- Core `compileall`: PASS.
- `git diff --check` in both repositories: PASS.
- Post-commit working trees: clean.

## Side-effect audit and limitations

```text
TD reads:                 bounded SELECT only
TD writes:                NONE
production Redis writes:  NONE
Rabbit consume/ACK:       NONE
systemd/service changes:  NONE
push/merge/deployment:    NONE
```

The repair does not prove real Rabbit arrival order, batch membership,
historical `available_at`, or full live/replay equivalence. It does not close
Phase P/TASK-008 acceptance and does not alter M3-1 production gates. Keep
Phase P/TASK-008 `PARTIAL`; no subsequent phase is promoted by this repair.
