# TASK-008 frozen-Q2 cutoff safety repair — 2026-09-25

## Scope and outcome

This bounded repair corrects the frozen-Q2 opening-validation runner only. It
does not change the Core Q2 contract, production path, stale-age freshness
threshold, Redis/TD/Rabbit access, or NORMAL/M3-1 gates. It applies the
already-stated whole-second precision to historical event-time comparison.

`Task008ReplayOpeningValidationV4` reports execution, projection
determinism, Engine comparability, and input quality separately. Before a
historical observation is submitted to Engine, the runner excludes quotes with
missing source time, a different trade date, or source event time later than
`observed_at` at whole-second precision. Both observed and source times are
compared after truncating subseconds; the exact requested observation time and
effective second cutoff are both recorded. The original unfiltered projection
remains in the report so its diagnostics are not hidden.
`historical_available_at_status` remains `UNKNOWN`: filtering by event time
does not prove when a Redis value was available.

Engine comparison requires at least one requested sample symbol to have an
eligible quote in both passes. Matching hashes over only missing sample facts
are `NOT_COMPARABLE`, not a deterministic Engine pass. Projection hashes are
still compared independently. CLI exit codes are `0=PASS`, `2=MISMATCH`, and
`3=NOT_COMPARABLE`; the JSON report records the same `exit_code`.

## Frozen real-capture validation

Input: `/home/exedev/validation/replay-20260918-0915-0940-20260920T014707+0800/redis_q2_capture.json`

- Input SHA-256: `21e00cc3a7730ffff114dfac531f58f72640198fdd0f622800c81ef02cf32f51`
- Rows / unique symbols: `5,224 / 5,224`
- Historical cutoff: `2026-09-18T09:32:10+08:00`
- Full projection: `PARTIAL`; `5,219 future_ts`, `5 stale`
- Event-time-eligible input quotes: `5 / 5,224`; requested sample quotes: `0 / 4`
- Ordered/shuffled projection determinism: `PASS`
- Engine comparison: `NOT_COMPARABLE`; all four requested facts are `MISSING`
- CLI exit code: `3` (`NOT_COMPARABLE`)
- `historical_available_at_status=UNKNOWN`
- `normal_opening_pass=UNPROVEN`
- `production_side_effects=NONE_OBSERVED`

Report directory:
`/home/exedev/validation/task008-gate-time-precision-repair-20260925.1sZox9/`

This validates safe handling of a genuine frozen Redis capture. It does not
prove that the capture represents Q2 as historically available at 09:32, nor
does it validate a real 09:32 opening analysis. The later source times are
excluded rather than treated as historical values; no replacement values are
invented.

## Verification

- Regression tests cover future/cross-date/unknown-time exclusion and prevent
  vacuous Engine comparison from being reported as a pass.
- Whole-second boundary and exit-classification regressions: targeted runner
  suite `16 passed`.
- Real frozen-capture replay generated an auditable report with projection
  determinism `PASS` and Engine comparison `NOT_COMPARABLE`.
- Full suite: `705 passed` (3 protobuf deprecation warnings).
- `compileall`: PASS.
- `git diff --check`: PASS.
- No live Redis, TDengine, RabbitMQ, service, or effect path was accessed.

## Mainline alignment

TASK-008 / Phase P remain `PARTIAL`; this is not an opening-validation pass and
does not start the next phase. `M3_1_NORMAL=BLOCKED` and
`TD_WRITE_HEALTH=UNPROVEN` are unchanged. Continue TASK-008 only with evidence
whose historical availability and source-time limits are honestly reported.
