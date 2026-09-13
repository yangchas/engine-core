# Legacy/core real TD anchor oracle (2026-09-13)

## Scope

The deployed Cobra release `e272842c8f490f55a1b017badb71e71904ce008e` was
used only for the pure `build_anchor_shadow_evidence` helper. Input rows came
from read-only TDengine `market_data1.auction_snapshot_v2` for `2026-09-10`,
symbol `600519`, tags `0920`, `0924` and `0925`. No Redis/TD writer, Rabbit
consumer, recovery path, notification or effect path was called.

## Observed legacy outputs

| Pair | Legacy status | Amount delta (yuan) | Price delta (milli) | Rest bid delta (yuan) | Rest ask delta (yuan) | Pressure delta (yuan) | Notes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 0920 -> 0924 | `unavailable` | `null` | `null` | `null` | `null` | `0.0` | 0920 price is missing; direction unresolved |
| 0924 -> 0925 | `resolved` | `6068560.0` | `200.0` | `-1419860.0` | `129200.0` | `-1549060.0` | amount ratio `2.0933475544`, pressure `6068560.0` in the old directional helper, label `volume_price_strengthening` |

The core endpoint facts for the same rows produce the same numeric price,
amount, resting-bid, resting-ask and endpoint-pressure deltas. The core keeps
the result fact-only and does not promote the old direction, withdrawal,
amount-bucket or threshold labels to a strategy contract.

## Parity classification

```text
endpoint numeric facts       = MATCH (real TD input)
pressure arithmetic           = MATCH (as a book proxy, not net flow)
legacy direction/ratio labels = UNKNOWN as a migrated strategy oracle
full production assembly      = UNKNOWN (missing same-date Redis summary,
                                  anchor and mapping evidence)
```

The old dictionary grouping behavior silently overwrites duplicate `(symbol,
tag)` rows. The core/probe boundary intentionally rejects duplicates so that
source corruption remains visible. That is an intentional safety change, not
a failed numerical parity result.

