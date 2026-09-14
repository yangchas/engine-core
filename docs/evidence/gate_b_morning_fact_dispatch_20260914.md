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
Core commit: d976f6d fix(shadow): reject ambiguous auction rows
Core feature commit: 41bc60d feat(shadow): dispatch morning fact nodes
Core archive: /home/exedev/validation/engine-core-d976f6d.tar
Archive SHA-256: 0b87120f043d1264218d771fed29c5ae6d1dd5e08aa5e07f902a0757cb8ebf0d
Local suite: 359 passed
Cobra-ion Python: 3.12.3
Cobra-ion suite: 359 passed
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
transition_exact: true only where the 0925 auction-change input was present;
                  300750 and 600519 remained without a comparable transition
                  input, not a fabricated mismatch
artifact SHA-256: 83ed7c0dca5655b528ece34aaaaf2cb4ad7af5fd024c716ba185f315caa442f4
```

This is an exact opening helper differential for the available fields.  It
does not establish fresh live coverage or full opening-transition parity.

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

normal AUCTION_0926: PARTIAL
normal OPENING_0932: UNAVAILABLE
recovery AUCTION_0926: PARTIAL
recovery OPENING_0932: UNAVAILABLE
```

`OPENING_0932=UNAVAILABLE` is fail-closed because the required comparable
auction-change input was not available.  It is not a provider exception and
not a reason to substitute `amount_yuan` or another field.

```text
Morning artifact SHA-256:
a754a288ed75221ab3d02f52b1c4386fe04828de66dc7320c6d5c4b1ea2e451a
```

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
