# Gate B legacy numeric auction parity (2026-09-18)

## Scope

This closes one capability-local parity slice between the old pure
`engine_next.runtime.auction_shadow.build_anchor_delta_evidence()` helper and
Core `AuctionFactShadow` using the same captured 600519 `0920 -> 0924` source
rows. It compares numeric facts only. It does not migrate the old
`direction`, `status`, `labels`, thresholds, strategy console, or delivery
behavior.

## Verified legacy values

The old helper, after the legacy row aliases are normalized, produced:

```text
price_delta_milli       = -2060
amount_delta_yuan       = 4407516
rest_bid_delta_yuan     = 648770
rest_ask_delta_yuan     = -129960
pressure_delta_yuan     = 778730
direction               = negative       (not migrated)
labels                  = volume_price_weakening (not migrated)
```

The Core differential test records the five numeric values as the expected
legacy oracle for the same fixture and obtains identical values from the
pure `AuctionFactShadow` wheel. The Core fact remains `FACT_ONLY`; it does not
turn the old directional label into a strategy conclusion.

## Evidence and verification

Source fixtures:

```text
tests/fixtures/facts/auction_600519_20260903.json
docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json
```

The parity test is
`test_core_auction_shadow_matches_verified_legacy_numeric_delta_slice`.
Local and cobra-ion isolated suites pass `508` with `compileall` passing. The
test does not import the old project at runtime; old code is used as the
verified oracle and recorded fixture evidence only.

```text
GATE_B_NUMERIC_AUCTION_PARITY = MATCH
GATE_B_DIRECTIONAL_STRATEGY_PARITY = NOT_MIGRATED
```

No Redis/TD writes, Rabbit changes, notification, or production service
changes were made.
