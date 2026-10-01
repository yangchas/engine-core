# TASK-008 opening amount cohort summary — 2026-10-01

Status: bounded Core fact slice `PASS_WITH_LIMITS`; overall
`TASK-008=PARTIAL_EVIDENCE` is unchanged.

## Why this slice

The deployed `engine_next/runtime/open_confirmation.py` consumes Q2
`amount_2m_yuan` as an opening fact. Its market aggregate selects rows whose
price-based opening fact is available and uses `_optional_sum`: the sum is
available only when every eligible row has a finite amount; a missing amount
is not treated as zero. Core previously retained the per-symbol Q2 value but
did not expose this cohort-level fact.

Core now provides `OpeningAmountSummaryV1` via
`build_opening_amount_summary()` and includes it in the Q2Frame opening shadow
report. It is observational only: partial amount coverage is reported, never
used to stop replay, and the report makes no full-market claim. It is not a
strategy conclusion and does not change Engine inputs or decisions.

## Real archived-data comparison

The audit used a saved Core opening report from an exact deployed-release
t1-v2 replay and the matching same-run producer `opening_cutoff_v1` command.
No live Redis, TDengine, or RabbitMQ connection was made.

```text
trade date / evaluation: 2026-09-29 / 09:32:10
Q2Frame SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
producer journal SHA-256: 3422c51fe911172cef247a0eb23ad98f7987a34325abc83db943bebf76f5993f
Core report SHA-256: 90429f5a7a69d29293fff626be0864f4a751f9ed3e96391bfe7a237db26785ee
fresh Core cohort / producer rows: 5,211 / 5,211
membership mismatch: 0
per-symbol amount_2m_yuan mismatch: 0
Core / producer amount sum: 34,487,835,732 / 34,487,835,732 yuan
field present / eligible: 5,211 / 5,211
```

The 5,211 rows are the fresh observed cohort matching the producer cutoff;
the broader Q2Frame had 5,223 observed symbols, including 12 stale rows. The
producer metadata says universe authority is partial, so these counts and the
sum are not asserted as complete full-market figures.

Machine-readable audit:
`/home/exedev/validation/task008-opening-amount-audit-20261001/audit-final-v2.json`
(`PASS_WITH_LIMITS`, SHA-256
`e9af0929242f43bcdc8c3ef12730580f6ba7114bd361cadefe69161f71ce1426`). The
audit consumes the frozen report and journal; it does not repeat the source
replay.

## Implementation and verification

- Added a pure amount-summary contract using the legacy price-valid denominator
  and complete-value sum behavior.
- Added zero-versus-missing, partial, empty, price-ineligible, determinism, and
  runner-report coverage tests.
- Bumped the additive Q2Frame opening shadow report contract to V6.
- Extended the pinned real-artifact audit with per-symbol and aggregate amount
  comparisons.

```text
targeted opening/runner tests: 30 passed
full Core suite: 770 passed, 3 existing protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production side effects: NONE
```

## Limits and alignment

The producer and Core values come from the same exact-release replay, not an
independent market oracle or live Redis observation. This verifies a useful
opening fact over that observed cohort; it does not prove historical
`available_at`, Rabbit delivery/order, full-market coverage, or NORMAL opening
acceptance. No freshness threshold or clock-alignment gate was tightened.
`TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged. No next task is auto-promoted.
