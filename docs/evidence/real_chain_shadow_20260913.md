# Real chain shadow — 2026-09-13

## Identity

- Core archive: `0fe64314ab76d95d31220f6695982c0ab2bdc8a1`
- Legacy runtime release: `e272842c8f490f55a1b017badb71e71904ce008e`
- Host: `cobra-ion`
- Python: `/home/exedev/services/engine-next/shared/venv/bin/python` (3.12.3)
- Trade date: `2026-09-10`
- Symbols: `600519`, `000001`, `000002`
- Mode: bounded read-only shadow; no producer, writer, ACK, repair or effect path

## Commands

The archive was streamed to an isolated `/tmp` directory on Cobra. The
following commands used the existing TD/Redis connection defaults and wrote
only temporary JSON files, which were removed after hashing:

```text
python examples/run_real_auction_shadow.py \
  --trade-date 2026-09-10 --symbol 600519 --output /tmp/auction.json

python examples/run_real_redis_td_projection_compare.py \
  --trade-date 2026-09-10 --symbols 600519,000001,000002 \
  --output /tmp/projection.json
```

## Results

### TD auction projection → core facts

The real `market_data1.auction_snapshot_v2` rows contained 0920, 0924 and
0925 for 600519. Source record times were retained as:

```text
0920  2026-09-10 09:20:03.323
0924  2026-09-10 09:24:10.365
0925  2026-09-10 09:25:06.078
```

The core fact path produced:

```text
0920→0924  PARTIAL  (0920 price unavailable)
0924→0925  READY
decision   FACT_ONLY / OBSERVE
```

The shadow content hash was
`abb13e59f6c087d20cd0fae9b92b29476db9e1cb0e1d0fd48222495b1a23fb32`.
This is source-formula and lineage evidence, not a legacy strategy oracle.

### Redis auction projection ↔ TD

For the three requested symbols and three tags, the Redis projection had no
comparable row on Cobra for this date. All nine field comparisons were
`NOT_COMPARABLE` with reason `redis_top_amount_unavailable`; there were no
`MISMATCH` rows. The result semantic hash was
`0f6407d9d695abce793df554fff7368e3909201186ba6c0fdf9a9c72b0df62a7`.

This does **not** prove Redis/TD equality or inequality. It proves only that
the current Redis retention/projection cannot serve as a same-date authority
for this bounded comparison. TD is not used as a substitute for Redis runtime
input.

## Gate classification

```text
TD_PROJECTION_READ_ONLY                 PASS
CORE_FACT_RECONSTRUCTION                PASS (PARTIAL/READY as above)
REDIS_TD_COMPARISON                     NOT_COMPARABLE
RABBIT_BATCH_MEMBERSHIP                 UNKNOWN
WRITER_PROJECTION_CONSISTENCY           UNKNOWN
LEGACY_FORMAL_STRATEGY_ORACLE           UNKNOWN
PRODUCTION_REPLACEMENT                  NOT CLAIMED
```
