# Startup readiness probe calendar-loader fix — 2026-09-14

## Scope

The read-only startup readiness CLI now accepts both the normalized
`TradingCalendarSnapshotV1` fixture shape and the real `RealCalendarProbeV1`
evidence emitted by the existing BaoStock probe.  The loader rebuilds the
immutable snapshot, derives explicit compatibility defaults only for fields
absent from the raw probe, and verifies any embedded semantic hash.

This is an operator-entrypoint compatibility fix only.  It does not add a
provider, change the Redis read path, consume RabbitMQ, write Redis/TD, or
alter production services.

## Verification

- Code commit: `670f639` (`fix(readiness): accept raw calendar probe evidence`)
- Local: `397 passed`; `compileall` passed; `git diff --check` passed.
- Cobra-ion (`Python 3.12`): `397 passed`; `compileall` passed.
- Real probe evidence loaded on Cobra-ion:
  - `calendar_id=CN_A_SHARE`
  - `timezone=Asia/Shanghai`
  - source guard `2023-12-01..2026-12-31`
  - semantic hash `a64a5dfa7a9e256c500799c2fa646f9e92b0a3d21d9b45c9f68d01ee1655610e`

## Safety

At verification time `engine-next` and `t1-v2-live` remained active.  The
scheduled read-only morning shadow was moved to the exact `engine-core-670f639`
archive and remains before-window; no output directory has been created.
