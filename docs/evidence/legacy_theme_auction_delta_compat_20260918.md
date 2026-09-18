# Legacy theme auction-delta compatibility verification — 2026-09-18

## Scope

This verification checks the explicit compatibility-only wheel
`build_legacy_theme_auction_delta_compat_facts` against the deployed
`engine-next` legacy theme aggregation helper. It is a differential Shadow
check, not a claim that the compatibility behavior is the preferred Core fact
contract.

The input was read-only Redis data from the existing
`market:auction:{date}:0924` and `market:auction:{date}:0925` projections,
joined with the existing Redis theme mapping views. No Redis/TD writes, Rabbit
operations, recovery, network fallback, notifications, or effects were
performed.

## Remote evidence

| Item | Value |
|---|---|
| trade date | `2026-09-18` |
| scope | `REDIS_TOP_AMOUNT_INTERSECTION` |
| common normalized rows | `175` |
| mapped rows | `170` |
| themes emitted by both paths | `119` |
| compatibility wheel | `LegacyThemeAuctionDeltaCompatV1` |
| artifact | `/home/exedev/validation/engine-core-886f418/theme-compat-differential-20260918.json` |
| artifact SHA-256 | `cbaef1aeb495a0efaa6e429e0d37187edc9403bdaac940516ad7b295aef6dc2e` |

## Differential result

All comparable fields matched for all 119 themes:

| Field | Compared | Matched | Different |
|---|---:|---:|---:|
| `amount_0925` | 119 | 119 | 0 |
| `amount_delta_24_25` | 119 | 119 | 0 |
| `amount_ratio_avg` | 119 | 119 | 0 |
| `bid_amount_delta_24_25` | 119 | 119 | 0 |
| `change_pct_delta_avg` | 119 | 119 | 0 |

## Interpretation

The compatibility wheel reproduces the currently deployed legacy numeric
aggregation for this bounded real-data input. It intentionally preserves the
legacy semantics that differ from the conservative Core fact wheel, including
zero-fill and the legacy weighting/averaging rules. It does not emit strategy
labels such as `温和放量`, and it must not be used as a silent replacement for
`ThemeAuctionDeltaFactV1`.

The result proves numeric parity only for the observed Redis TopN intersection.
It does not prove full-universe coverage, historical availability, producer
batch equivalence, or strategy/report parity.

## Next permitted use

Use this wheel only to build the first bounded Auction Shadow differential:

```text
same real rows
→ legacy-compatible numeric facts
→ compare with engine-next numeric output
```

Keep production ownership and all strategy/effect migration gates unchanged.

## Final Core commit read-only shadow

The final Core commit `763164b` was deployed to a fresh Cobra-ion validation
directory and ran the existing Redis projection shadow without changing the
production services. The current read-only result was:

| Item | Value |
|---|---|
| artifact | `/home/exedev/validation/engine-core-763164b/theme-shadow-final-20260918.json` |
| artifact SHA-256 | `d7d396061e9df7ca7d85f2279dc5d247471e6aa060e6a1da3157263fbbc34117` |
| projection status | `0924=READY`, `0925=READY` |
| normalized rows | `175` |
| mapped rows | `170` |
| missing mappings | `5` |
| fact count | `119` |
| result status | `OBSERVED` |
| read-only | `true` |

This run confirms the final commit can consume the current real Redis
projection path. It remains bounded TopN Shadow evidence and is not a claim of
full-universe or production replacement readiness.
