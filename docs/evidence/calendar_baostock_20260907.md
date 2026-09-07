# BaoStock Trading Calendar Probe

日期：2026-09-07 交易日准备阶段

## Source

- provider: BaoStock 00.9.30
- host: cobra-ion
- runtime: Python 3.12.3, `/home/exedev/services/engine-next/shared/venv`
- access: 复用既有 BaoStock login/session；只读调用 `query_trade_dates`
- query range: `2023-12-01` through `2027-01-31`
- evidence ref: `calendar://cn-a-share/baostock-20260907-v1`

## Observed result

- response rows: 1127
- trading dates: 748
- source returned through: `2026-12-31`
- requested 2027 guard dates were not returned by the source and were not fabricated
- fixture: `tests/fixtures/calendar/baostock_cn_a_share_20260907.json`
- fixture SHA-256: `0FE13FFD22710F43C2D7EB758CE9795F21DFA5CD113989C01129E132248F87EF`

## Runtime contract

The fixture declares formal decision coverage `2024-01-01..2026-12-31` and the
observed source guard coverage `2023-12-01..2026-12-31`. The missing 2027 guard
range is an explicit source-coverage limitation, not a fallback opportunity.
The snapshot semantic hash excludes source/session/evidence details; those are
recorded separately in the evidence hash.
