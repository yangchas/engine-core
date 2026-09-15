# Real PreviousDayStats readiness — 2026-09-15 11:15 CST

The existing Core `PreviousDayStatsFunction` was executed against the
deployed TD read path for symbols `000001`, `000002`, and `600519`.

```text
requested trade_date       2026-09-15
calendar-derived previous  2026-09-14
actual source              tdengine_daily_kline
rows seen                  0
result status              MISSING
completeness               0.0
available_at_ms            null
observed_at                2026-09-15T11:13:12+08:00
```

The calendar semantic hash was
`cad3aa73a6726b57739241156ea3a0f178d06dd6d8c081224c43deb172f3bc56`.
The provider received the exact calendar-derived date and did not substitute
an earlier date. No fallback, repair, Redis/TD write, Rabbit operation, or
notification occurred (`TD SELECT only`).

This is a genuine runtime observation. At this time the current day's
previous-day daily-kline rows were not available through the verified TD path;
Core therefore correctly returned `MISSING` instead of manufacturing a
`READY` result or using an unverified fallback. The absence blocks a complete
reference-data bundle for a live decision, but does not invalidate the date
derivation or provider boundary.
