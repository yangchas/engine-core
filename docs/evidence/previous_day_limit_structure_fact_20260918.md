# Previous-day limit-up structure fact

## Scope

Commits `4ca5915` and `bfbfd85` add and harden the pure
`PreviousDayLimitStructureFact` wheel. It
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
- The Cobra-ion real runner now emits the structure projection in
  `runtime-observations/limit-pool-structure-1519.json`; copied artifact
  SHA-256 is `5ae6704b48da85dbeb43ae6992d23d2b078c45ab41173082c99a8e022fb3c6d7`.
  Its real result is `UNAVAILABLE` with `row_count=null` because the source
  still lacks historical availability evidence; this is the intended
  fail-closed result.

## Verification

- Local: `486 passed`; compileall passed.
- The mapping carrying board-height counts is recursively frozen after hash
  construction; mutation is rejected by regression test.
- Local: `488 passed`; compileall passed.
- Cobra-ion isolated copy under Python 3.12.3: `488 passed`; compileall passed.
- New source/test/fixture hashes matched local and remote exactly.
- No Redis/TD write, Rabbit change, service restart, notification, or effect
  occurred.

## Parity boundary

This closes only previous-session structural fact extraction. It does not
claim parity for current-session return feedback, plate aggregation, locked
orders, mapping/anchor lifecycle, or the full legacy report.
