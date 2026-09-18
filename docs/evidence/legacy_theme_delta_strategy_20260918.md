# First Auction Shadow rule — 2026-09-18

## Scope

The first strategy-shaped migration is the pure legacy theme delta signal
rule extracted from `engine_next/strategy_skill_layer/auction_plate_buckets.py`
(`_infer_snapshot_delta_signal`). It consumes only numeric output from
`LegacyThemeAuctionDeltaCompatV1` and has no data access, state, fallback,
notification, or effect behavior.

The rule is intentionally named and versioned as legacy compatibility. It does
not redefine the conservative Core fact contract and does not migrate the
surrounding theme ranking, collision, opening-confirmation, or report logic.

## Exact rule order

```text
amount_delta >= 50,000,000 and change_delta_avg >= 1.0
    → 增量转强
amount_delta >= 50,000,000 and change_delta_avg <= -2.0
    → 放量回落
bid_delta >= 10,000,000 and amount_delta >= 0
    → 封单增强
amount_delta <= -20,000,000
    → 竞价降温
amount_ratio_avg >= 1.5 and amount_delta > 0
    → 温和放量
otherwise
    → 平稳
```

Boundary tests cover all thresholds, precedence, zero/positive sign behavior,
and rejection of `None`/non-finite inputs. Missing-value zero-fill remains an
explicit responsibility of the legacy compatibility fact builder; the pure
rule itself refuses invalid numeric inputs.

## Migration boundary

This is ready for a bounded fact-only Shadow comparison using the real Redis
0924/0925 TopN intersection. It is not an authorization to replace
`engine-next`, emit strategy output, or infer a final trade decision.

## Real-data input check

The exact Core commit `d8f4f6c` ran the existing read-only Redis 0924/0925
projection shadow on Cobra-ion. The input produced 175 normalized rows, 170
mapped rows, and 119 theme facts (82 `READY`, 37 `PARTIAL`). Both projections
were `READY`, the overall result was `OBSERVED`, and the script reported its
read-only boundary. Artifact:

```text
/home/exedev/validation/engine-core-d8f4f6c/theme-shadow-final-20260918.json
SHA-256: f03fa1f227e8ed263f4383e158ed09c9058067ac138eb7043a684e1f5a432248
```

This is a real-data input exercise for the pure rule, not a legacy strategy
output comparison and not a production effect test.
