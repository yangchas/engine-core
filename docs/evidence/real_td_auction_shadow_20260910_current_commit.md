# Real TD auction fact shadow — current commit

## Scope

The current core commit was run on `cobra-ion` against the production
TDengine `market_data1.auction_snapshot_v2` read path. The query was bounded to
`600519` and tags `0920`, `0924`, and `0925`. The calculation used only the
existing `engine_core` fact wheels; it did not call Redis, RabbitMQ, recovery,
writers, notifications, or strategy effects.

## Identity

| Item | Value |
| --- | --- |
| Core commit | `c9912a5` |
| Runtime | `/home/exedev/services/engine-next/shared/venv/bin/python` |
| Artifact | `/tmp/core-shadow-c9912a5/auction-shadow-20260910-600519.json` |
| Artifact SHA-256 | `99646FD66E61A4AE13CA72302967D18627B8E5F246D23BBA4B0D71BA993EE350` |
| Side effects | none (`read_only=true`) |

## Source rows

| Tag | Business anchor | Source record time | Price (milli) | Match amount (yuan) | Rest bid (yuan) | Rest ask (yuan) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 0920 | 09:20:00 | 09:20:03.323 | unavailable | 3,485,160 | 0 | 0 |
| 0924 | 09:24:00 | 09:24:10.365 | 1,290,800 | 5,550,440 | 1,548,960 | 0 |
| 0925 | 09:25:00 | 09:25:06.078 | 1,291,000 | 11,619,000 | 129,100 | 129,200 |

The source record timestamp is preserved separately from the business anchor;
it is not treated as Rabbit arrival time or batch order.

## Fact output

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
```

The first segment is partial because the 0920 source row has no price. Theme
and breadth remain unavailable for this single-symbol sample. No strategy
threshold, buy/sell label, EV, or net-flow claim is produced.

The two segment hashes, comparison hash, semantic shadow hash and evidence
hash are present in the artifact and are deterministic for this exact input.

## Interpretation boundary

This is a real TD projection fact test and proves the current core can consume
three non-empty production rows without side effects. It does **not** prove
Rabbit/runtime batch membership, producer freeze causality, Redis/TD writer
identity, full-market coverage, or replacement readiness for `engine_next`.
