# TASK-008 handoff: opening plate price breadth and median

Date: 2026-10-01 (Asia/Shanghai)

## Scope

Add the opening-side plate price facts that the legacy `open_confirmation`
consumer reports but the Core plate amount summary did not: up/down/flat
counts, positive/negative ratios, and median opening change. The comparison
cohort is the intersection of the frozen plate mapping, frozen auction
membership, and available Core opening facts. A missing/invalid change is
excluded from the ratio and median denominator and counted separately.

Core exposes this as `OpeningPlatePriceSummaryV1` and attaches it to the
existing 09:32 Q2Frame opening report when the same explicit, date-pinned
plate context is supplied. It is descriptive `FACT_ONLY` evidence; it adds no
strategy classification, no new freshness/universe gate, and no producer or
production access.

## Real replay comparison

Result: `PASS_WITH_LIMITS` — 10 selected plates, 10 compared opening-side
fields per plate, zero mismatches against the matching legacy output.

The audit runner
`examples/audit_task008_real_opening_plate_price_summary.py` verifies the
input checksums and re-computes the summary from the saved Core facts and
frozen context. The pinned inputs are the real 2026-09-29 exact-release
t1-v2 Q2Frame replay, its integrated Core report, frozen mapping/auction
membership context, and same-run legacy `open_confirmation` output. The
Q2Frame was not regenerated or filtered for this comparison.

- Q2Frame SHA-256: `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`
- Core report SHA-256: `fe1b04b0956dff3593ec6b157c60a68bd23004c1bd9a14e853940f8432bb921b`
- Legacy report SHA-256: `9c338e2b2f53675f488c5bac714806692e3b15f0d12533a218e8e57d70474704`
- Plate context SHA-256: `7c5f01017174f96689ab3d0f3e8cf3cdfbd8da57eb4546eec17fb1b256e3f1c6`
- Compared fields: open valid/common/comparison counts, up/down/flat counts,
  positive/negative ratios, median change, and open-symbol membership.
- Mismatches: `0` across all 10 legacy-selected plates.
- Core summary hash: `e55d03fbea613cedc8754e28d41aabe1c1b5190287f9c7aa321464b0e75a4bab`.

Evidence directory:
`/home/exedev/validation/task008-opening-plate-price-20261001T220000+0800/`

- `opening_plate_price_summary.json`
- `audit_summary.json`
- `sha256sums.txt`

Both evidence-file checksums were verified successfully.

## Verification and limits

- Focused opening/runner tests: `42 passed`.
- Full Core suite: `795 passed`, with 3 existing upstream protobuf
  deprecation warnings.
- `compileall`: PASS.
- `git diff --check`: PASS.
- The audit used frozen local real replay artifacts; it made no new Redis,
  TDengine, or RabbitMQ connection. Production side effects: `NONE`.
- This proves parity for the pinned date, release, cohort, and listed fields;
  it does not prove full-market membership, original Rabbit arrival order, or
  historical `available_at`.
- This summary does not yet include auction-to-open plate deltas; those require
  separately explicit auction-side positive-ratio/median inputs. No auction
  values are inferred here.

## Alignment

This closes one missing opening-side fact surface in the Core Q2Frame report.
It is a real-data-backed migration increment, not full TASK-008 acceptance.
Keep `TASK-008=PARTIAL_EVIDENCE`; do not promote another phase from this result.
Next alignment should verify the original plan and identify whether the next
useful seam is the auction-side inputs needed for plate deltas or a different
evidenced Core consumer field. Do not add unavailable auction values or turn
unknown completeness into a processing stop.
