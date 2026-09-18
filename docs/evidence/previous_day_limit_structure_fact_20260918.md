# Previous-day limit-up structure fact

## Scope

Commit `4ca5915` adds the pure `PreviousDayLimitStructureFact` wheel. It
derives only previous-session membership structure from a guarded
`previous_day_limit_pool` result:

- `row_count`
- `highest_board_height`
- `highest_board_symbols`
- `board_height_distribution`

It deliberately does not derive current-session return feedback, plate
strength, or a strategy conclusion. A source result that is `UNAVAILABLE`,
`MISSING`, `INVALID`, or otherwise unsafe never leaks its rows into the fact.

## Real source evidence

- Cobra-ion read-only Redis key: `cache:yest_limit_pool:2026-09-17`.
- `HLEN` before/after scan: `47/47`; scan rows `47`; decode errors `0`.
- Source metadata has no verified historical `available_at_ms`; the Core
  `PreviousDayLimitPoolFunction` therefore returned `UNAVAILABLE` with
  `missing_fields=("available_at_unknown",)`.
- The captured JSON fixture is
  `tests/fixtures/data/previous_day_limit_pool_20260917_real.json`, SHA-256
  `00c6376d1ca8587c15be7aadb5ff3f08d7c76f5cc45db0d782d1788daa290c89`.

## Verification

- Local: `486 passed`; compileall passed.
- Cobra-ion isolated copy under Python 3.12.3: `486 passed`; compileall passed.
- New source/test/fixture hashes matched local and remote exactly.
- No Redis/TD write, Rabbit change, service restart, notification, or effect
  occurred.

## Parity boundary

This closes only previous-session structural fact extraction. It does not
claim parity for current-session return feedback, plate aggregation, locked
orders, mapping/anchor lifecycle, or the full legacy report.
