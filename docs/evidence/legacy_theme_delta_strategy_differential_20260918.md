# Legacy theme delta strategy differential — 2026-09-18

## Scope

This is a bounded read-only differential on the real Redis 0924/0925 TopN
intersection. It runs the deployed `engine-next` pure
`build_auction_snapshot_delta_stats` helper and the Core pair:

```text
LegacyThemeAuctionDeltaCompatV1
→ infer_legacy_theme_delta_signal
```

The old helper and the Core wheel are given the same normalized business
values. The validation adapter maps Core canonical names explicitly to the old
function's legacy input names (`amount`, `amount_delta`, `bid_amount_delta`,
`change_pct_delta`, `amount_ratio`). It does not rely on same-looking field
names or default missing values.

## Result

| Item | Value |
|---|---:|
| trade date | `2026-09-18` |
| projection status | `0924=READY`, `0925=READY` |
| normalized rows | `175` |
| mapped rows | `170` |
| themes compared | `119` |
| read-only | `true` |
| result status | `OBSERVED` |

All five numeric fields matched 119/119:

```text
amount_0925              119 / 119
amount_delta_24_25       119 / 119
amount_ratio_avg         119 / 119
bid_amount_delta_24_25   119 / 119
change_pct_delta_avg     119 / 119
```

The signal also matched 119/119. There were no mismatch samples.

```text
artifact:
/home/exedev/validation/engine-core-f744ea5/theme-strategy-differential-20260918.json
SHA-256:
faf86074375634786d8b42aa633e3cdd2cfc808852579fd9e50176e29010e085
```

## Audit note

An initial validation attempt passed canonical Core field names directly to
the legacy helper. Because the legacy helper intentionally reads different
keys and defaults absent keys to zero, that attempt produced false zero-valued
differences. It was discarded and not used as evidence. The corrected run
uses an explicit, tested field mapping and closes the differential.

This proves bounded numeric and label parity only. It does not prove full
universe coverage, runtime batch equivalence, report parity, or production
replacement readiness.
