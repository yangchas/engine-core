# Live morning shadow evidence — 2026-09-17

## Scope

This run was a bounded, read-only validation on `cobra-ion`.  The production
owners remained `engine-next` and `t1-v2-live`; no Rabbit consumer or ACK path
was changed, and the shadow did not write Redis/TD or emit notifications.

Runtime copy:

```text
/home/exedev/validation/engine-core-2414eed-v1
```

Command window:

```text
trade date: 2026-09-17
symbols: 000001, 000002, 600519
start: 09:15:00
stop: 09:33:00
nodes: AUCTION_0926, OPENING_0932
auction reference prefetch: enabled
origin: RECOVERY_CATCHUP
```

## Source and readiness observations

The startup probe observed a Q2 cohort with `coverage=1.0`, but the quality
state was not promoted to READY.  The node probes reported
`BEST_EFFORT_MIXED_FRESHNESS` and `PARTIAL`; the oldest and newest source
timestamps were retained as evidence.  Coverage therefore means that rows
were read, not that every row was fresh or semantically complete.

The prefetch preparation was bound to the Engine evaluations through
`ENGINE_DATA_READY`.  The reference content hash was:

```text
6d0d8c594d741cbc11734ad81d8a8b4c4d0cc3cfdb085ac7cca90ef742d4d57b
```

Reference availability was not upgraded from unknown/observed evidence.  The
run continued to produce truthful PARTIAL/UNAVAILABLE facts where required.

## AUCTION_0926

The wall deadline fired 62 ms late and was marked `RECOVERY_CATCHUP` because
the shadow process started after the requested 09:15 boundary.  All three
symbols executed the in-memory Engine path:

```text
processed_signals: 9 per symbol
strategy_result_count: 3 per symbol
reference_binding: ENGINE_DATA_READY
semantic_hash_equal: true
```

`000001` and `000002` produced READY 0924→0925 anchor-delta facts.  `600519`
remained PARTIAL/UNAVAILABLE for missing auction fields.  The facts retain
the 0920, 0924 and 0925 source-record timestamps; no source time was rewritten
to the business anchor.

## OPENING_0932

The wall deadline fired 93 ms late and was marked `RECOVERY_CATCHUP`.  Q2 was
read again at the node boundary and remained PARTIAL despite full row
coverage.  Opening transition facts were:

```text
000001: READY
000002: READY
600519: UNAVAILABLE (auction reference fields unavailable)
```

The opening path was read-only and used the Q2 projection directly; no
notification or external effect was attempted.

## Cross-environment evidence hashes

The files were copied from the remote output directory without modification.
Remote and local SHA-256 values matched exactly:

| file | SHA-256 |
| --- | --- |
| `startup.json` | `edcd43e18958c13160a2dea3b06178eded07e8e5b57e7951dffa28fd098c2bb9` |
| `AUCTION_0926.json` | `7bc960e30cfa60a68d396dc8a49621d9927f9deb29b0fda7f21444b26adae4aa` |
| `OPENING_0932.json` | `402685a00e4d470e9f1a4669860791759691027fedfe1c9ac1bb47da475e6c83` |
| `manifest.json` | `388019f2f972f816b27669a70590b4263f55312e026790277bac0758728ee10a` |

Local archive:

```text
engine_core/tmp/live-morning-shadow-20260917-2414eed/
```

## Production safety status

At completion:

```text
engine-next: active, MainPID=657653, NRestarts=0
t1-v2-live: active, MainPID=2878024, NRestarts=0
new Rabbit consumer: 0
Rabbit ACK/publish: 0
Redis write: 0
TD write: 0
notification/effect: 0
production restart: 0
```

## Acceptance

```text
SOURCE_INGESTION_ACCEPTANCE: UNKNOWN/OBSERVED
  runtime batch membership is not exposed by the current production logs.

AUCTION_STATE_ACCEPTANCE: PASS for bounded shadow facts; PARTIAL for 600519.
STORAGE_PROJECTION_ACCEPTANCE: OBSERVED (read-only source evidence captured).
ENGINE_NEXT_CONSUMPTION_ACCEPTANCE: UNKNOWN (no dedicated read trace in this run).
ENGINE_CORE_SHADOW_ACCEPTANCE: PASS for execution and semantic-hash parity;
  source quality remains PARTIAL/UNAVAILABLE where recorded.
JOINT_TRADING_DAY_ACCEPTANCE: WARN
```

This evidence does not authorize replacing `engine-next`.  The next migration
step remains M1 startup/readiness parity and then differential validation of a
single verified Auction Shadow rule.
