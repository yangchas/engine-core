# Real TD previous-day probe — 2026-09-13

## Identity

- Core archive: `76a8a50ff6a260d934513f015176aa07d8f05bf3`
- Host: `cobra-ion`
- Python: `/home/exedev/services/engine-next/shared/venv/bin/python` (3.12.3)
- Requested trade date: `2026-09-10`
- Calendar-derived previous trade date: `2026-09-09`
- Symbols: `600519`, `000001`, `000002`
- Mode: TD `SELECT` only; no writes, Redis, Rabbit, repair or effects

## Observed result

The thin `TDPreviousDayStatsProvider` returned three real
`market_data1.daily_kline` rows for the calendar-derived date. Core
normalization preserved the close/amount values and explicit zero volumes:

```text
rows_seen              3
actual_trade_date      2026-09-09
missing_symbols        []
completeness           0.0
result_status          UNAVAILABLE
missing_fields         [available_at_unknown]
available_at_ms        null
```

The `UNAVAILABLE` result is intentional: the source table has no historical
publication/availability evidence. The query observation time is retained as
`observed_at_ms` only and is not promoted to `available_at_ms`.

The temporary output artifact SHA-256 was
`e7129ae697f7408d49ea89404a679ff23a56231845bb6be378f7922edcdacbc2`.

## Gate classification

```text
TD_CONNECTIVITY                         PASS
CALENDAR_DATE_DERIVATION                PASS
ROW_NORMALIZATION                        PASS
UNKNOWN_AVAILABILITY_FAIL_CLOSED         PASS
PREVIOUS_DAY_RUNTIME_READINESS           NOT_READY
PRODUCTION_REPLACEMENT                   NOT_CLAIMED
```

