# Real engine-next read-path probe — 2026-09-15 11:12 CST

This is a bounded, read-only comparison against the deployed
`engine-next@/home/exedev/services/engine-next/releases/20260903_e272842`
release. The probe used the existing Redis read path and legacy pure fact
function with recovery, network, writer, Rabbit, notification, and effect
hooks disabled. `guard_writes=[]`.

## Context probe

Symbols: `000001`, `000002`, `600519`; phase: `intraday`; requested previous
trade date: `2026-09-14`.

The legacy context path returned real current Q2-derived rows:

```text
000001  auction_amount=2404200   current_pct=-0.0025316455696201556
000002  auction_amount=1489900   current_pct=-0.006557377049180357
600519  auction_amount=20239800  current_pct=-0.0010720210335222191
```

Legacy pure fact rows were produced for the same bounded input. Quote health
reported `future_source_timestamp=false`, latest quote age about `4631 s`,
and latest quote timestamp `1789437289000`. The data is therefore real but
far outside the live freshness budget. This is not a current-live-ready
signal.

## Auction loader probe

The existing read-only `load_auction_snapshots()` path was queried for today's
`0920`, `0924`, and `0925` tags and the same three symbols. It returned:

```text
row_count=0
row_count_by_tag={0920: 0, 0924: 0, 0925: 0}
source=empty
duplicate_row_keys=[]
guard_writes=[]
```

No current-day auction projection was available through this loader at the
observation time. The empty result must not be replaced with yesterday's
projection or reconstructed as a normal auction segment.

## Interpretation

```text
REAL_ENGINE_NEXT_READ_PATH       PASS (bounded, side-effect-free)
LEGACY_CONTEXT_FIELDS            OBSERVED (real Q2, stale)
LEGACY_AUCTION_PROJECTION        UNAVAILABLE (today's tags empty)
CORE_VS_LEGACY_EXACT_PARITY      UNPROVEN (different freshness/auction input)
PRODUCTION_WRITES                0
RABBIT_ACK_OR_CONSUMER_CHANGE    0
NOTIFICATION/EFFECT              0
```

This closes another real read-path observation but does not close normal
09:26/09:32 in-session evidence, auction writer parity, or the Core replacement
gate. The per-symbol fact `status=available` remains a field-level statement;
the batch freshness and auction projection status must remain visible to any
future report consumer.
