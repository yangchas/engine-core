# Real PreviousDayStats evidence — 2026-09-18

## Scope

The Core `PreviousDayStatsFunction` was run on `cobra-ion` with the existing
TDengine daily-kline query shape, for requested trade date `2026-09-18` and
derived previous trade date `2026-09-17`. The query was bounded to
`000001`, `300750`, and `600519` and used TD `SELECT` only.

Artifact: `previous-day-stats-20260918.json`  
SHA-256:
`b62bcc6b7b37a50594c988c909657714f86ae6cafbda5488e2679c76dabe774c`.

## Observed result

```text
rows_seen = 3
actual_source = tdengine_daily_kline
actual_trade_date = 2026-09-17
requested_trade_date = 2026-09-18
close rows = 3
amount rows = 3
missing symbols = 0
available_at_ms = null
result_status = UNAVAILABLE
completeness = 0.0
```

The previous trade date was derived by the Core calendar authority, not passed
through a second context field. The source returned real rows and values, but
the deployed data has no historical publication/availability attestation, so
the temporal contract correctly kept the result `UNAVAILABLE` with
`missing_fields=["available_at_unknown"]`. Today’s observation time was not
used to fabricate historical availability.

The returned `volume` values were zero in this TD sample and are not promoted
to a shares/lots interpretation by this evidence. The volume-unit contract
remains separate and unresolved.

## Safety

No Redis write, TD write, repair, Rabbit operation, notification, or effect was
assembled. This closes the real TD provider/read path for the bounded sample;
it does not yet make the reference data eligible for historical replay or
replace `engine-next` startup behavior.
