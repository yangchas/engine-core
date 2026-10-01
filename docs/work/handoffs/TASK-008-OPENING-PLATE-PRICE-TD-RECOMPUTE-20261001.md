# TASK-008 follow-up: independently recompute opening plate auction prices

Date: 2026-10-01 (Asia/Shanghai)

## Result

```text
RAW_TD_AUCTION_RECOMPUTE=PASS_WITH_LIMITS
Q2_PREVIOUS_CLOSE_SCALE_CROSSCHECK=3350_CHECKED_0_MISMATCH
CORE_OPENING_SIDE_PARITY=10_PLATES_0_MISMATCH
CORE_AUCTION_DELTA_FORMULAS=10_PLATES_0_MISMATCH
LEGACY_AUCTION_MEDIAN_UNIT_DIVERGENCE=10_OF_10_PLATES
TASK-008=PARTIAL_EVIDENCE
```

This follow-up supersedes the strength of the auction-side parity claim in
`TASK-008-OPENING-PLATE-PRICE-DELTAS-CORE-20261001.md`. It does not invalidate
the Core calculation code: the earlier audit passed legacy auction values
into Core and then compared the resulting output to that same legacy artifact.
That was a useful wiring/calculation check, but it was not an independent
check of the auction input or its unit.

## Independent source calculation

The audit runner now streams the hash-pinned captured rows from
`market_data1.auction_snapshot_v2` (`td_auction_snapshot_rows.jsonl`), uses the
exact hash-pinned selected membership from `opening_plate_amount_context.json`,
and derives auction positive ratio and median from the 09:25 rows. It does not
copy auction metrics from `legacy_open_confirmation.json` into Core.

Final evidence directories:

- `/home/exedev/validation/task008-opening-plate-price-td-recompute-20261001T211459+0800/`
- `/home/exedev/validation/task008-opening-plate-price-td-recompute-20261001T211459-repeat+0800/`

The two final runs' `sha256sums.txt` files are byte-identical, and both runs
reported the same Core summary hash. Earlier exploratory audit output
directories are retained and are not the final runner version.

- TD rows: 15,669 total; 5,223 for each of 09:20, 09:24, and 09:25; SHA-256
  `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`.
- Mapping snapshot: 5,963 symbols; SHA-256
  `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b`.
- The frozen analysis context contains 1,321 symbols across its 10 selected
  plates. Every selected context membership agrees with the broader mapping
  snapshot. The broader snapshot has additional symbols for several plates;
  this audit deliberately preserves the exact pre-existing context cohort
  instead of silently expanding it. This is not a full-market claim.
- No duplicate `(auction_tag, symbol)`, malformed row, invalid symbol,
  unexpected tag, or trade-date mismatch was observed.
- Per-plate raw valid counts and positive ratios exactly match the legacy
  artifact for all 10 plates.
- `chg_bp` was independently reconstructed from the first pinned Q2Frame's
  previous-close field using C++ truncation-toward-zero semantics. There were
  3,350 overlapping valid rows checked and zero mismatches; 1,235 eligible TD
  rows had no usable previous-close value in that Q2Frame and remain outside
  this arithmetic cross-check.
- Source-file hashes for the inspected t1-v2 calculator/writer and
  engine-next normalizer/distribution/consumer match their recorded hashes.
  The deployed producer binary/build is still `UNVERIFIED`.

## Unit finding

The TD column `chg_bp` is basis points. The checked t1-v2 calculation derives
it as the signed price change multiplied by 10,000 and divided by previous
close; the TD writer persists it in `chg_bp INT`. The checked engine-next TD
normalizer converts this field to percentage points using `chg_bp / 100`, and
the plate distribution consumes that value with `change_pct_unit="percent"`.
The raw Q2Frame cross-check independently corroborates that scale on the
3,350 overlapping symbols.

Across all 10 selected plates, the legacy field named
`auction_median_change_pct` equals the raw median `chg_bp`, not the normalized
percentage-point median. Examples:

| Plate | Raw median (bp) | Correct normalized median (percentage points) | Legacy field |
| --- | ---: | ---: | ---: |
| 通信 | -39 | -0.39 | -39.0 |
| 光模块 | -13 | -0.13 | -13.0 |
| 医药 | -10 | -0.10 | -10.0 |
| 风电 | -109 | -1.09 | -109.0 |

Thus the captured legacy artifact has a 100x median-unit divergence in all
10 plates. Its metadata says `data_origin=replay_fixture_only` and
`auction_source.observation_time=unavailable`; this is evidence about this
artifact, not proof that a live production report made the same error.

## Core result and limits

With raw TD-derived percentage-point inputs, the Core V2 summary matched all
10 legacy opening-side fields on all 10 selected plates. Its auction-side
positive ratio, normalized median, deltas, and descriptive states matched an
independent formula recomputation for all 10 plates. Mismatch counts are zero.
The report status is `CORE_TD_RECOMPUTE_PASS_WITH_LEGACY_UNIT_DIVERGENCE`.

This is a calculation check against pinned captured historical evidence, not
a new live TD/Redis/Rabbit query. TD completeness remains `UNKNOWN`; original
Rabbit arrival order and historical `available_at` remain `UNKNOWN`; the
Q2Frame is event-time replay; selected plate membership is not full-market
coverage; and the deployed t1-v2 producer build is not attested. Production
side effects: `NONE`.

## Reproduction and files

```bash
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/audit_task008_real_opening_plate_price_summary.py \
  --output-dir /home/exedev/validation/task008-opening-plate-price-td-recompute-20261001T211459+0800
```

The output directory contains:

- `auction_price_stats_from_td.json`
- `opening_plate_price_reference_context.json`
- `opening_plate_price_summary.json`
- `audit_summary.json`
- `sha256sums.txt`

The previous same-artifact audit directory is retained as historical
evidence; it is not rewritten. The audit runner and unit test now derive the
auction values from the captured TD rows and explicitly preserve the
basis-points-to-percentage-points conversion.

Verification on the final code:

- Focused TASK-008 auction/Q2Frame tests: `12 passed`.
- Full Core suite: `799 passed`, with three existing protobuf deprecation
  warnings.
- `compileall`: PASS.
- `git diff --check`: PASS.
