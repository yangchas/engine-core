# Real TD auction fact shadow — 2026-09-10 13:14 CST

The same `engine_core` commit was run on `cobra-ion` against the read-only
`market_data1.auction_snapshot_v2` projection for `600519`.  The input is a
TD auction projection, not a raw tick or Rabbit batch log.

## Source anchors

| Business anchor | Source record time | Price (milli) | Match amount (yuan) | Rest bid (yuan) | Rest ask (yuan) |
| --- | --- | ---: | ---: | ---: | ---: |
| `AUCTION_0920` | `2026-09-10 09:20:03.323` | missing | 3,485,160 | 0 | 0 |
| `AUCTION_0924` | `2026-09-10 09:24:10.365` | 1,290,800 | 5,550,440 | 1,548,960 | 0 |
| `AUCTION_0925` | `2026-09-10 09:25:06.078` | 1,291,000 | 11,619,000 | 129,100 | 129,200 |

The business anchors remain `09:20:00`, `09:24:00`, and `09:25:00`; source
record timestamps are preserved and are not rewritten to those anchor times.

## Fact result

The two adjacent segment frames were built without Engine or online Redis:

| Segment | Coverage | Frame hash |
| --- | --- | --- |
| `09:20 → 09:24` | `PARTIAL` (0920 price unavailable) | `8b8ac704e6855ca3f1076000dc5d9048a06de8729d569c6e1baace7a081127d6` |
| `09:24 → 09:25` | `READY` | `d4c02abf641e89e36aa80d6945c69d8f3728a3bdb2086a8ea6f0b9530a720347` |

The fact-only adjacent comparison returned:

```text
status: PARTIAL
price_delta_milli: 200
amount_delta_yuan: 6,068,560
rest_bid_delta_yuan: -1,419,860
rest_ask_delta_yuan: 129,200
pressure_delta_yuan: -1,549,060
decision_status: FACT_ONLY
```

The pressure change is an endpoint order-book proxy.  It is not labelled or
interpreted as net capital flow.  The first frame's missing price remains
missing; no zero fill or fallback was applied.

## Identity and safety

```text
core_commit: 7fbc556
archive_sha256: 028F0EB4FFF68263902E969626FE63DF1DF1B1494DDA4A0C20136E68B852C93B
output_sha256: 24E1649DD171E823D5D339C17738C98631A19C77F5D72138A3064E9594BCAE42
shadow_content_hash: abb13e59f6c087d20cd0fae9b92b29476db9e1cb0e1d0fd48222495b1a23fb32
shadow_evidence_hash: 9d13639d170e456e1ac177c9761aa1c0f0286d3d6305a4b2e68c16ef84ea2f5c
```

The command performed TD `SELECT` only.  It did not write Redis/TD, consume
Rabbit, invoke repair, send notification, or execute a strategy/effect.

This evidence closes the real TD projection fact path only.  It does not
prove Rabbit arrival order, producer batch membership, or that the TD
projection is an immutable historical snapshot.
