# Legacy calendar parity audit (2026-09-13)

## Scope and authority

This is a wheel-local behavior audit, not a claim that the legacy calendar is
the correct business authority. The inspected legacy implementation is
`web/services/trading_calendar_service.py` in the current workspace. Runtime
production behavior still requires a release-bound source audit.

## Legacy behavior observed

| Capability | Legacy behavior | Core contract | Parity status |
| --- | --- | --- | --- |
| Parse date | `datetime.strptime(value, "%Y-%m-%d")`; errors are caught by callers and usually become `False` or the input date | strict `date` or strict `YYYY-MM-DD`; `datetime` rejected | `INTENTIONAL_CHANGE` |
| Trading-day source | Monday-Friday minus `holidays.CN()`; no exchange make-up Saturday rule | versioned `TradingCalendarSnapshot` built from observed source dates | `INTENTIONAL_CHANGE` |
| Previous day | scans at most 30 days; on failure returns the requested date | searches the immutable snapshot; out of coverage raises `CalendarCoverageError` | `INTENTIONAL_CHANGE` |
| Previous trading-day alias | same scan, but failure returns current date minus 30 days regardless of status | one `previous_trade_day` authority | `INTENTIONAL_CHANGE` |
| Next day | scans at most 30 days; failure returns current date plus 30 days | searches guard coverage; out of coverage raises | `INTENTIONAL_CHANGE` |
| Latest completed day | wall clock; a trading day is complete at 15:30 local | timezone-aware `as_of` and caller-supplied `completion_cutoff_time`; equality completes | `MATCH` for the explicit cutoff rule |
| Trading time | inclusive string comparisons for 09:30-11:30 and 13:00-15:00; wall-clock default | session scheduling is separate from the calendar snapshot | `NOT_APPLICABLE` |
| Recent days | wall-clock scan with a one-year safety break | not part of the first calendar wheel | `NOT_APPLICABLE` |

## Important differences

The core deliberately does not preserve legacy failure fallbacks that return a
date which has not been proven to be a trading day. In particular, an input
outside declared or source-guard coverage is fail-closed instead of silently
returning the input or a +/-30 day guess. This is a semantic correction, not a
requirement to reproduce a legacy bug.

`completion_cutoff_time` is a data-readiness policy supplied by the caller; it
is not a fact contained in the calendar source. The core does not infer market
session boundaries from this helper.

## Evidence and remaining unknowns

The core calendar fixture and the Cobra BaoStock probe have an identical
canonical trading-date set for the audited range. That proves canonicalization
and cross-environment date identity, but it does not prove that the legacy
`holidays.CN()` implementation matches exchange make-up days outside the
captured source range. No runtime calendar provider is changed by this audit.

```text
legacy_calendar_behavior = OBSERVED
core_snapshot_contract   = VERIFIED for the supplied snapshot
legacy_calendar_full_parity = UNKNOWN
```

