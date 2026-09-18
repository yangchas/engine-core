# Auction Engine Shadow — 2026-09-18 16:21 CST

## Scope

Bounded real-data verification of the existing auction fact path. The runner
read only `market_data1.auction_snapshot_v2` for `600519` and submitted the
three source anchors through the public Core Engine signal path. It did not
read/write Redis, consume RabbitMQ, write TDengine, send a report, or invoke an
effect.

## Result

```text
trade_date                 2026-09-18
symbol                     600519
processed_signals          6
strategy_result_count      3
engine_fact_status         PARTIAL
engine_fact_only           true
semantic_hash_equal        true
engine_fact_content_hash   3904bb7d4a331bdfff9e38c8d26f8d1871752954ec41c482f05bac5d3de9e276
```

The source timestamps were preserved:

```text
0920  1789694403287
0924  1789694650292
0925  1789694706197
```

The direct pure fact and Engine-composed fact have the same semantic hash;
their evidence hashes differ because the Engine path carries its own trace
lineage. This is the expected `EXACT_EQUIVALENCE` result for the same TD
projection and the same information granularity. It is not Rabbit arrival
ordering proof and not a production-owner replacement acceptance.

Artifact copied without editing:

```text
tmp/real-reference-20260918/auction-engine-20260918-1621.json
SHA256=e0b34ec6f5f8740a20b39acd73605253db806f220c2eddd08cba0451c14ed4ef
```

## Gate conclusion

```text
REAL_TD_AUCTION_READ_PATH       PASS
WHEEL_ENGINE_PARITY             PASS
FACT_ONLY_BOUNDARY              PASS
SOURCE_BATCH_EQUIVALENCE        UNKNOWN (TD has no Rabbit batch identity)
CORE_REPLACEMENT                NOT READY
```
