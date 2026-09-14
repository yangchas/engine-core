# Real reference-source probe — 2026-09-14 18:06 CST

## Scope

The existing `engine-next` connector implementations were called directly on
`cobra-ion` for one bounded sample (`trade_date=2026-09-11`, `symbol=600519`,
`max_rows=3`). This was a read-only connectivity and response-shape probe; no
Redis/TD writer, repair, notification, SMTP or strategy component was
assembled.

```text
legacy release: 20260903_e272842
Python: 3.12.3
connection calls: 6/6 PASS
artifact SHA-256: ff8d875b9a8a1d50134280be0160df99085c822c45c2a6d6fcfffd63210f5bbd
```

## Observed results

| source / getter | returned | date evidence | runtime role |
|---|---:|---|---|
| Baostock `fetch_daily_kline` | 1 row | request and response both `2026-09-11` | candidate dated reference data |
| Kaipanla `fetch_hot_plates` | 3 rows | request date only; response is not self-dated | `OBSERVED`, not historical runtime proof |
| Kaipanla `fetch_yesterday_bans_pool` | 3 rows | request date only; no response date contract | `OBSERVED`, not historical runtime proof |
| Kaipanla `fetch_ban_reasons` | 1 row | no structured source trade date | `OBSERVED`, not historical runtime proof |
| Wencai `fetch_limitup_with_lb_days` | 3 rows | current query has no structured date | `OBSERVED`, historical replay `UNAVAILABLE` |
| THS `fetch_hot_rank` | 3 rows | current query has no date | `OBSERVED`, historical replay `UNAVAILABLE` |

The returned Kaipanla/THS/Wencai labels contain encoding artifacts in the
legacy response, which is retained as evidence rather than silently repaired.
The probe confirms connector reachability and field shapes only. It does not
prove units, historical publication time, or business semantic parity.

## Contract impact

```text
connector connectivity                  PASS (6/6)
Baostock request/response date contract  PASS
other source response observation       OBSERVED
historical available_at evidence         UNKNOWN
safe for replay runtime                  Baostock candidate only; others NO
side effects                             none observed / read-only boundary
```

`observed_at` records when this probe retrieved the response. It is not an
`available_at` claim. For replay, a result with unknown historical
`available_at` remains unavailable under `TemporalDataGuard`; the response may
be used for oracle comparison or frozen fixture capture only.
