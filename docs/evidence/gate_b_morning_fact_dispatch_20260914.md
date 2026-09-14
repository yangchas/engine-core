# Gate B Morning Fact Dispatch Evidence — 2026-09-14

## Scope

This checkpoint adds only a thin, read-only composition boundary for the
Core-owned morning nodes.  It does not add a scheduler, workflow engine,
retry loop, persistence, strategy effect, Rabbit consumer, or production
writer.

```text
AUCTION_0926  -> AnchorDeltaFactV1 (0924 -> 0925)
OPENING_0932  -> OpeningFactV1 / OpeningTransitionFactV1
```

The dispatch function consumes timer evidence and calls the already tested
fact wheels.  Normal and `RECOVERY_CATCHUP` evidence are kept as separate
origin scopes.  It is not a claim that the production `engine-next` timer
consumer has been replaced.

## Code and verification identity

```text
Core commit: b241a70 fix(opening): distinguish non-comparable inputs
Typed-unit fix: ca05ddf fix(opening): honor typed auction change units
Previous boundary fix: d976f6d fix(shadow): reject ambiguous auction rows
Core feature commit: 41bc60d feat(shadow): dispatch morning fact nodes
Core archive: /home/exedev/validation/engine-core-b241a70.tar
Archive SHA-256: a975cd4a99f8540f6cf57b0dc235c3bb677cbe448dacf457289be337f3ca089e
Local suite: 373 passed
Cobra-ion Python: 3.12.3
Cobra-ion suite: 373 passed
compileall: PASS
```

The local and Cobra runs used the same archive contents.  The test suite is
fixture/self-contained; the production-source results below are reported
separately.

## Real Cobra opening differential

Command scope was read-only Redis Q2 plus read-only TD
`auction_snapshot_v2`, compared with the deployed release at
`/home/exedev/services/engine-next/current`.  No Redis/TD write, Rabbit ACK,
notification, recovery, or effect was performed.

```text
trade_date: 2026-09-14
symbols: 000001, 300750, 600519
projection: coverage=1.0, status=STALE,
            consistency=BEST_EFFORT_STALE
opening_exact: true for all three symbols
transition_exact: true for 000001; 300750 and 600519 remained without a
                  comparable transition input, not a fabricated mismatch
artifact SHA-256: 78f2fff501a19224fb089c203bb8cc253ef64c9b8201eca4536f49f265390902
```

This is an exact opening helper differential for the available fields.  It
does not establish fresh live coverage or full opening-transition parity.

### 0925 change input lineage closure

The TD row contract was inspected against the deployed production normalizer:

```text
TD auction_snapshot_v2.chg_bp = -8
production normalize_td_auction_row -> change_pct = -0.08
Core normalize_auction_change_bp_to_pct -> -0.08
```

The previous generic ratio normalizer was not appropriate for this typed TD
column and could turn `-8` into `-8.0` percentage points.  The typed conversion
is now a separately tested Core wheel.  For 300750 and 600519 the raw TD
`chg_bp` is genuinely null and both production/Core remain unavailable.

### Bounded 0925 transition differential

The current release was queried read-only for the first 100 deterministic
symbols returned by the 0925 TD projection, then joined to the real Redis Q2
projection:

```text
symbols examined: 100
opening helper exact: 100/100
transition exact on comparable rows: 99/99
non-comparable source rows: 1 (000016, chg_bp is null)
transition mismatches: 0
```

The runner reports missing transition input as `NON_COMPARABLE` rather than
`MATCH`; this keeps source-fact absence visible without manufacturing a
mismatch.  Batch artifact SHA-256:

```text
a87ca104273198887df88dd9b1d5cc17ff343004a9c6ceddcc5786e4d314c5da
```

## Real Q2 capture Morning Shadow

Input was the real captured file
`/home/exedev/audit/production_ground_truth/20260914/q2_093210.jsonl` and the
read-only TD auction projection.  The capture contains 5220 symbols and was
observed at 09:32:10 CST.

```text
Q2 quote count: 5220
coverage: 1.0
consistency: BEST_EFFORT_MIXED_FRESHNESS
source-time range: 2026-09-14 00:00:00 -> 09:31:14 CST
TD rows for 600519: 3

normal AUCTION_0926: READY
normal OPENING_0932: READY (000001; auction_change_pct=-0.08)
recovery AUCTION_0926: READY
recovery OPENING_0932: READY (000001; auction_change_pct=-0.08)
```

`OPENING_0932=UNAVAILABLE` is fail-closed because the required comparable
auction-change input was not available.  It is not a provider exception and
not a reason to substitute `amount_yuan` or another field.

```text
Morning artifact SHA-256 (000001):
181063f60e34be6ddd7f797552c6ebc377df78514a0fdda7d8ac4fbc84e7102c
```

The same capture for 600519 remains `AUCTION_0926=PARTIAL` and
`OPENING_0932=UNAVAILABLE` because its TD 0925 price/change fields are absent;
the missing source fact is not synthesized.

The timer evidence at a late observation time contains both normal due and
recovery-catchup views.  The two views are evidence scopes, not two production
executions.

The final code also rejects cross-symbol auction rows and duplicate auction
tags at this composition boundary instead of silently letting the last row
overwrite an earlier row.  Boundary tests cover both cases.

## Production-chain shadow from the capture

The current code also rebuilt the existing capture-only chain report in an
isolated validation directory:

```text
engine_core_q2_path: PASS (repeat hash equal)
source_ingestion: UNKNOWN (no Gateway batch membership)
auction_state: OBSERVED
storage_projection: WARN
engine-next consumption: UNKNOWN
engine_core shadow: PARTIAL
joint trading day: WARN
```

No TD tick file was present in this capture directory, so tick-shape and raw
TD auction fact evidence were not upgraded.  The report artifact SHA-256 is:

```text
b2ff6004233c0d363339da7b1760324d86babcbd4a1290d75513283fe811ed30
```

## Safety and status

At verification time on Cobra-ion:

```text
engine-next: active, MainPID=4022407, NRestarts=0
t1-v2-live: active, MainPID=2878024, NRestarts=0
root filesystem: 19G total, 16G used, 2.2G available (88%)
```

The current Core status remains **shadow-only**.  This checkpoint proves
fact-node composition and real read-only source handling, not replacement of
`engine-next`.  Remaining blockers include a complete 0924/0925 source
contract, production timer/batch evidence, engine-next loader/report parity,
and multi-day shadow stability.
