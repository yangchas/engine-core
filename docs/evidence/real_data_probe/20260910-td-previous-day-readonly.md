# 2026-09-10 TD previous-day read-only probe

## Scope

This probe exercised the real cobra-ion TD connection through the thin
`TDPreviousDayStatsProvider` boundary.  The query was read-only and selected
three known symbols from `market_data1.daily_kline`; it did not initialize TD
objects, write TD/Redis, acknowledge Rabbit messages, or restart a service.

The provider callable used the existing production Python 3.12 `taos` client
and a bounded `SELECT` with the requested date and symbol list.  The core
function supplied the requested trading date to the calendar and derived the
previous date itself.

## Runtime and request

| Item | Observed value |
|---|---|
| Host | `cobra-ion` |
| Python | `/home/exedev/services/engine-next/shared/venv/bin/python` (3.12 runtime) |
| TD database | `market_data1` |
| Table | `daily_kline` |
| Requested trade date | `2026-09-09` |
| Derived previous trade date | `2026-09-08` |
| Symbols | `000001`, `000002`, `600519` |
| Probe observed_at | `2026-09-10T09:08:49.193+08:00` |

## Result

```text
read_only                 = true
derived_previous_trade_date = 2026-09-08
returned_row_count        = 3
missing_symbols           = []
available_at_ms           = None
status                    = UNAVAILABLE
completeness              = 0.0
missing_fields            = ["available_at_unknown"]
content_hash              = f019da1cbbb351c5228fd1c5d764fe10c2f5a926d4d2e9bb5e360a6817560327
```

The three rows were obtained from the real TD table and normalized without
changing explicit values.  `UNAVAILABLE` is the expected temporal result:
`daily_kline` has no verified historical publication/availability timestamp,
so a successful query at 09:08 cannot prove that the data was knowable at an
earlier replay cutoff.  `observed_at` remains audit metadata only.

## Contract conclusion

```text
real_td_connection                 PASS
calendar_previous_date_derivation  PASS
provider_read_only                 PASS
requested_symbol_completeness      PASS (3/3 rows)
unknown_available_at_fail_closed   PASS
production_side_effects            0
```

This evidence verifies the real access path and date contract.  It does not
promote the result to runtime-ready data; a future READY result still requires
verified `available_at_ms <= knowledge_as_of_ms`.
