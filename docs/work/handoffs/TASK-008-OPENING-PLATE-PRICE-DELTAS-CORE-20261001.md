# TASK-008: opening plate price deltas in Core

Date: 2026-10-01 (Asia/Shanghai)
Implementation base: `d57f34f4e4b54435f653823eed9fb00185d681ae`

## Result

Added `OpeningPlatePriceReferenceV1` as an optional, date- and cohort-pinned
input to the opening price summary. Core now reports the frozen auction-side
positive ratio and median change alongside the opening-side facts, their
deltas, and the legacy descriptive states. Missing reference metrics stay
unavailable; this remains `FACT_ONLY` and does not gate opening evaluation.

The summary contract is now `OpeningPlatePriceSummaryV2`. The Q2Frame runner
keeps the old V9 path when only the existing amount context is supplied, and
uses V10 only when the new price reference context is supplied. A CLI option
loads that context without adding any production data-source or write path.

## Frozen-artifact audit

Audit output:
`/home/exedev/validation/task008-opening-plate-price-delta-20261001T204614+0800/`

```text
status=PASS_WITH_LIMITS
trade_date=2026-09-29
plates_compared=10
fields_per_plate=16
mismatches=0
side_effects=NONE
```

The audit verifies the pinned Q2Frame, integrated Core report, legacy report,
plate context, prior parity report, frozen mapping snapshot, and captured TD
auction-row manifest. It compares Core opening values and auction-to-open
delta/state outputs to the pinned legacy outputs. The emitted context,
summary, and audit hashes are recorded in that directory's `sha256sums.txt`.

Important limit: the auction price reference values in this small audit are
copied from `legacy_open_confirmation.json`, not independently regenerated
from the captured TD rows. The legacy artifact itself reports
`data_origin=replay_fixture_only` and auction `observation_time=unavailable`.
So this is a same-date, hash-pinned calculation/parity check; it is not proof
of historical availability, live-source timing, original Rabbit order, or
full-market coverage. A separate small follow-up can independently derive
those auction metrics from the frozen TD rows and compare them before stronger
claims are made.

The existing Q2Frame is a real t1-v2 event-time replay, but not original Rabbit
arrival order. M3-1 / TD write-health remains outside this feature and
unchanged. No live Redis, TDengine, RabbitMQ, production service, or effect
path was accessed or modified in this implementation.

## Verification

- Targeted opening and Q2Frame runner tests: `45 passed`.
- Full suite: `798 passed` (three upstream protobuf deprecation warnings).
- `compileall`: PASS.
- `git diff --check`: PASS.
- New tests cover reference context date/cohort pinning, explicit unavailable
  inputs, delta/state semantics, V10 report inclusion, and repeated-run
  determinism.
- Full suite, compileall, and diff-check results are recorded at commit time.

## Current assessment

This closes the Core calculation seam for displaying auction-to-open price
breadth/median deltas against an explicit frozen reference. It does not close
all of TASK-008 or validate the reference computation independently from raw
TD rows. Keep TASK-008 at its existing partial-evidence status; do not infer
NORMAL readiness from this audit.
