# TASK-008 — opening price and auction-pressure current-code audit

Date: 2026-10-08 (Asia/Shanghai)

## Result

The current Core working-tree code was re-run against hash-pinned 2026-09-29
historical evidence. Opening price-summary artifacts were byte-identical on
repeat. Core's per-symbol anchor-pressure formula matched the hash-pinned
engine-next release helper on all 5,223 observed symbols, and all ten selected
plate pressure values/statuses matched. The quality denominator did not match
for eight plates because the two paths used different universes.

```text
OPENING_PRICE_SUMMARY_REPEAT=PASS
RAW_TD_AUCTION_PRICE_RECOMPUTE=PASS_WITH_LIMITS
ANCHOR_PRESSURE_SYMBOL_FORMULA_PARITY=5223/5223
PLATE_PRESSURE_VALUE_STATUS_PARITY=10/10
PLATE_PRESSURE_DENOMINATOR_PARITY=2/10
OVERALL_PRESSURE_PARITY=VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE
PRODUCTION_ASSEMBLY_GATE=NOT_REPLAYED_MISSING_SAME_DATE_REDIS_ANCHOR
TASK_008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

This is a feature-scoped calculation audit, not a live production report
acceptance or a reason to stop unrelated Core development.

## Pinned real inputs

Input directory:
`/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/`

| Input | SHA-256 | Use |
|---|---|---|
| `td_auction_snapshot_rows.jsonl` | `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b` | Captured real `market_data1.auction_snapshot_v2` rows |
| `stock_plate_snapshot.json` | `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b` | Frozen 2026-09-29 mapping, 5,963 symbols |
| `opening_plate_amount_context.json` | `7c5f01017174f96689ab3d0f3e8cf3cdfbd8da57eb4546eec17fb1b256e3f1c6` | Selected cohort: 1,321 symbols across 10 plates |
| `deployed_release_q2frame_to_0932.jsonl` | `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9` | Same-date t1-v2 Q2Frame event-time replay |

The auction capture contains 15,669 rows, 5,223 for each of 0920, 0924, and
0925, with no duplicate tag/symbol pairs or malformed rows in the retained
capture. This verifies those bytes and their internal counts; it does not prove
TD had no upstream omissions. The Q2Frame's first anchor contains 3,497
previous-close values for the arithmetic cross-check; exact producer
binary/build attestation remains `UNVERIFIED`.

## Current Core opening price summary

The current audit runner independently derives auction reference values from
the captured 0925 TD rows and compares the Core summary with the pinned opening
report and raw-derived formulas:

```text
selected plates: 10
open price / limit-state rows: 657 / 657 valid
limit-up: 5
normal: 652
limit-down: 0
Core opening comparison mismatches: 0
Core raw formula mismatches: 0
Q2 previous-close arithmetic checks: 3,350; mismatches: 0
TD rows without usable previous close in Q2: 1,235
auction-pressure captured source rows used: 10,446 (0924 + 0925)
pressure summary mismatch against pinned legacy values: 0
```

Both current-code runs reported the same summary hash
`b2878ba04e49fd425d59bde8ef44ea3da811aa3675297dd10faf10b9f3b531f8`.
Their `sha256sums.txt` and `audit_summary.json` were byte-identical. The
opening comparator artifact labels itself `data_origin=replay_fixture_only`
and has no observation time. Therefore zero opening-field mismatches are a
wiring/regression comparison, not independent proof of the true live 09:32
opening cohort. The independently grounded source calculation is the auction
side derived from the captured TD rows.

## Independent Core versus engine-next helper parity

`audit_task008_auction_pressure_production_parity.py` imported only pinned
pure helpers from release `/home/exedev/services/engine-next/releases/20260903_e272842`.
Recorded engine-next source hashes matched the expected values. The audit
reported:

```text
normalized 0924/0925 rows: 10,446
symbol formula parity: 5,223 matched / 5,223; 0 value mismatches
plate pressure values: 0 mismatches / 10 plates
plate pressure statuses: 0 mismatches / 10 plates
plate quality denominators: 8 mismatches / 10 plates
overall: VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE
```

The frozen selected-plate mapping contains 1,412 members; the exact pinned
opening context selects 1,321. Of the 1,412 selected mapping members, 91 do
not appear in the captured auction-row cohort. Core uses the full frozen
selected mapping as its quality denominator, while the release helper's
`stock_count` reflects symbols observed in the captured rows. The counts
reconcile exactly with that set difference. It is not established whether
those 91 symbols were absent from the effective TD universe, absent from the
same-date Redis auction anchor, or absent only from this captured query.
Consequently, do not silently force either denominator to the other.

The pure calculation does not replay the engine-next production assembly gate:
same-date Redis auction-anchor membership and effective-TD-universe evidence
were not included. The pinned release requires those universes to match before
emitting the plate shadow. The gate result remains
`NOT_REPLAYED_MISSING_SAME_DATE_REDIS_ANCHOR`; final production report status
for 2026-09-29 is not inferred.

Both pressure parity audit runs produced byte-identical `sha256sums.txt` and
`audit_summary.json`. Hashes:

```text
pressure audit summary:
  e7c9817877c24ad34769efcc8d3687e4a0efc91960cfc4b71d107999bbc923c2
pressure audit sha256 manifest:
  7093c18b651f4a658174aedee4c02354333415a7e8393214aae2cd0da58239af
```

Evidence directories:

- `/home/exedev/validation/task008-opening-plate-price-current-code-20261008/`
- `/home/exedev/validation/task008-opening-plate-price-current-code-20261008-repeat/`
- `/home/exedev/validation/task008-auction-pressure-current-parity-20261008/`
- `/home/exedev/validation/task008-auction-pressure-current-parity-20261008-repeat/`

## Verification and side effects

```text
Core full suite: 834 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
```

All audits read pinned local validation artifacts and perform pure calculations.
They did not connect to Redis, TDengine, RabbitMQ, or a production service; no
production data or files were changed. No code was committed, pushed, merged,
or deployed. The worktree was already dirty at base HEAD
`a18654e10af663c48dc0571c5baa6c457baebaf0`; current tested source fingerprints
are recorded in the respective `audit_summary.json` files.

## Alignment and next useful evidence

The real calculations are usable for Core feature development with explicit
feature-scoped denominators and `PARTIAL` states. The eight denominator
differences are not a value-math failure and are not a global stop condition.
Before claiming final production assembler parity, obtain same-date Redis
auction-anchor membership and the effective TD universe for 2026-09-29 (or a
new controlled date with both captured). If unavailable, keep only the helper
parity claim and continue with features whose correctness does not depend on
that universe gate. Do not relabel the legacy fixture as live truth or
reinterpret the 91 symbols as source loss without the missing universe
evidence.

Historical tool-path note (2026-10-09): the interim helper
`audit_task008_auction_pressure_production_parity.py` and its paired test
were later moved out of the Core repository into
`/home/exedev/validation/task008-tools-archive-20261009T1355+0800/`.
The archive manifest records their checksums and restoration paths; this
contemporaneous report describes the helper location at the time of that run.
