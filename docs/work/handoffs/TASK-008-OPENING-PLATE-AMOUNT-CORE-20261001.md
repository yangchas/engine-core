# TASK-008 opening plate-amount Core slice

Date: 2026-10-01 (Asia/Shanghai)

## Status

`REAL_HISTORICAL_PLATE_AMOUNT_PARITY=PASS_WITH_LIMITS`
`CORE_OPENING_PLATE_AMOUNT_FUNCTION=IMPLEMENTED_AND_TESTED`
`TASK-008=IN_PROGRESS`

This closes one fact-aggregation slice only. The new Core summary is not yet
part of the normal Q2Frame shadow report path and does not establish live
Rabbit/Redis behavior, NORMAL opening acceptance, full-market authority, or
completion of TASK-008.

## Contract and implementation

Added pure `OpeningPlateAmountSummaryV1` aggregation in
`src/engine_core/opening.py`, exported from `engine_core`. It preserves the
deployed engine-next denominator split:

- total open-window `amt2m` sums all mapped symbols with an available opening
  fact;
- open Top1/Top3 concentration uses only the intersection of mapped symbols,
  valid 09:25 auction symbols, and available opening facts;
- a missing amount is not zero; incomplete sums remain `partial` and do not
  emit a complete amount or concentration ratio;
- full-market coverage remains `UNPROVEN` and no strategy conclusion is
  produced.

The read-only audit runner is
`examples/audit_task008_real_opening_plate_amount.py`. Its TD connection now
requires explicit `TDENGINE_HOST`, `TDENGINE_PORT`, `TDENGINE_USER`, and
`TDENGINE_PASSWORD`; it has no implicit local/root credential fallback. The
parity comparator rejects blank or duplicate plate identifiers rather than
silently overwriting rows during dictionary indexing.

## Real historical evidence

Evidence directory:
`/home/exedev/validation/task008-opening-plate-amount-20261001/`

Inputs and scope:

- TD `market_data1.auction_snapshot_v2`, SELECT-only, trade date 2026-09-29;
  15,669 rows, 5,223 each for `0920`, `0924`, and `0925`.
- Same-date frozen `market:stock_plate` snapshot: 5,963 mapping rows,
  effective at `2026-09-29T08:30:30.943852`; 5,155 auction symbols overlap the
  mapping in each anchor. Unmapped and mapping-only symbols are reported as
  scope limits, not treated as proof of full-market completeness.
- Exact-release t1-v2 Q2Frame through 09:32:10 and the matching Core opening
  report. Their SHA-256 values are recorded in
  `opening_plate_amount_parity.json` and `sha256sums.txt`.
- Historical event-time replay is not the original Rabbit delivery or
  processing sequence; `historical_valid=false` remains explicit in the
  legacy observation.

Result: the 10 plates selected by the legacy report matched across 16 amount,
cohort, ratio, status, and symbol fields per plate: `mismatch_count=0`,
`STRICT_VALUE_MATCH`, `PASS_WITH_LIMITS`. The saved evidence was re-read after
the comparator hardening: 10 plates, zero mismatches. All five evidence-file
hashes verified against `sha256sums.txt`.

## Verification

- Focused tests after adding duplicate-ID and explicit-connection checks:
  `31 passed` (`tests/test_task008_opening_plate_audit.py` and
  `tests/test_opening.py`).
- Full suite after the final audit harness changes: `778 passed` (3 existing
  protobuf deprecation warnings).
- `compileall -q src tests examples`: PASS.
- `git diff --check`: PASS.

The real-data audit wrote only to the validation directory. Its recorded side
effects are TD `SELECT_ONLY`, zero Redis writes, no Rabbit consume/ACK, no
service change, and no effects.

## Remaining gaps / aligned next step

1. Add this contract to the ordinary Core Q2Frame replay/shadow report through
   explicit, date-pinned auction/mapping inputs; do not silently read current
   market state or recompute Q2.
2. Re-run the same-date real replay with the integrated report and compare its
   output to the frozen evidence above.
3. Only then audit TASK-008 against its wider replay-for-development goal.

No TASK-008 task-board state was changed. M3-1 remains outside this slice;
`M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` are unchanged.
