# Previous-day limit-up feedback fact — 2026-09-18

## Scope

This slice extracts the objective part of the deployed `engine-next`
`_build_prior_limit_up_structure` path.  It joins a guarded previous-session
limit-up pool with already-normalized current `0925` rows and reports return
counts, median return, board-height structure, and per-symbol lineage.

It does not fetch Redis/TD, infer a timestamp, calculate a plate score, or
make a strategy decision.  `change_pct` is an explicit percentage-point
field.  It is not derived from `price`, `amount_yuan`, or an unrelated Q2
field.

## Contract

```text
PreviousDayLimitFeedbackFactV1
previous_result.status ∈ {READY, PARTIAL}
current row:
  trade_date == current_trade_date
  tag == 0925
  symbol == six digits
  change_pct = percentage points when present
  source_record_time_ms = positive source timestamp when present
```

Missing current return values remain unavailable and are not converted to
zero.  Missing source timestamps prevent the fact from claiming `READY`; the
business anchor (`0925`) and source record time remain separate fields.

## Verification

- Local targeted tests: 9 passed.
- Full local suite after this slice: 497 passed (expected count after the
  new fact tests).
- `compileall` and `git diff --check`: PASS.
- The real 47-row Redis capture for `2026-09-17` remains `UNAVAILABLE` for
  runtime because its historical `available_at` is unknown; the feedback
  fact therefore does not leak those rows into a runtime result.  The real
  payload remains available as offline evidence/fixture only.

## Parity status

The objective aggregation is aligned with the audited legacy path for the
fields currently covered: prior-pool membership, board height, current
0925 `change_pct`, up/down/flat counts, ratio, and median.  Plate aggregation,
formal current-return authority, and strategy thresholds remain outside this
slice and are not claimed as migrated.

Intentional safety differences are recorded rather than hidden:

- malformed or duplicate current rows fail closed instead of using a last-row
  wins interpretation;
- a missing return or missing source-record timestamp yields `PARTIAL` rather
  than the legacy `available` label;
- plate grouping is not copied until its mapping authority is verified.
