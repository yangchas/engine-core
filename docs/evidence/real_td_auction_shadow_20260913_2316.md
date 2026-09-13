# Real TD auction fact shadow — 2026-09-13 23:16 CST

## Scope

The exact `engine_core` archive at commit `20fbe959d041d7028d4f6f28847fc9109fb79138`
was executed on `cobra-ion` with the production shared Python 3.12.3 runtime.
The probe issued a bounded read-only `SELECT` for `600519` in
`market_data1.auction_snapshot_v2`, requesting tags `0920`, `0924`, and `0925`.
No Redis write, TD write, Rabbit consumer/ACK, repair, notification, or effect
path was imported or called.

## Verification identity

```text
host       = cobra-ion
python     = /home/exedev/services/engine-next/shared/venv/bin/python (3.12.3)
timezone   = Asia/Shanghai
hash seed  = 0
run 1 file = /tmp/core-live-auction-20260913-2327.json
run 1 sha  = e7a06126d3d9d89e5aeb66f6400771363d5a21297341ac2c111c55126d31c7d0
run 2 file = /tmp/core-shadow-20260913-2316.json
run 2 sha  = a9977c6fd994474bef5356bc4f0b7d7ddf01b2a35c0a152090e3d2f9cdc2ba41
```

The artifact hashes differ because each output records a different observation
wall time.  The semantic fact hashes and evidence hashes below are identical
across the two executions.

## Real source rows

| tag | business anchor | source record time | price (milli) | match amount (yuan) | rest bid (yuan) | rest ask (yuan) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 0920 | 09:20:00 | 09:20:03.323 | missing | 3,485,160 | 0 | 0 |
| 0924 | 09:24:00 | 09:24:10.365 | 1,290,800 | 5,550,440 | 1,548,960 | 0 |
| 0925 | 09:25:00 | 09:25:06.078 | 1,291,000 | 11,619,000 | 129,100 | 129,200 |

The source timestamp is retained as `source_record_time_ms`; it is not treated
as Rabbit arrival time, batch order, or a rewritten business anchor.

## Stable core fact result

```text
segment 0920→0924       coverage=PARTIAL
segment 0924→0925       coverage=READY
shadow status            PARTIAL
decision status          FACT_ONLY
state                    OBSERVE
price delta (milli)      200
amount delta (yuan)      6068560
rest bid delta (yuan)    -1419860
rest ask delta (yuan)    129200
pressure delta (yuan)    -1549060
comparison hash          c15356ca50e1e98ec03d80b9d5e88484b11ee6f0b5f84632f4655742575fe4dd
shadow content hash      abb13e59f6c087d20cd0fae9b92b29476db9e1cb0e1d0fd48222495b1a23fb32
shadow evidence hash     9d13639d170e456e1ac177c9761aa1c0f0286d3d6305a4b2e68c16ef84ea2f5c
```

`price` remains unknown for the 0920 row and is not zero-filled.  Breadth and
theme are unavailable for this single-symbol input.  The pressure value is an
endpoint order-book proxy, not a claim about net capital flow.

## Gate classification

```text
REAL_TD_READ_PATH                 PASS
CORE_FACT_RECONSTRUCTION          PASS (PARTIAL/READY)
REPEAT_SEMANTIC_HASH              PASS
REPEAT_EVIDENCE_HASH              PASS
RABBIT_BATCH_MEMBERSHIP           UNKNOWN
PRODUCER_FREEZE_CAUSALITY         UNKNOWN
REDIS_TD_WRITER_IDENTITY          UNKNOWN
FORMAL_AUCTION_STRATEGY_ORACLE    UNKNOWN
PRODUCTION_REPLACEMENT            NOT_CLAIMED
```

This is a real production TD projection read and fact-wheel verification.  It
does not prove that TD is a raw Tick stream, that the 0925 final tick was in a
particular Rabbit batch, or that `engine_core` can replace `engine_next`.
