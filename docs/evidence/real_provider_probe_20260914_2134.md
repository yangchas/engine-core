# Real provider probe — 2026-09-14 21:34 CST

## Scope

The current Core archive was executed on cobra-ion against the existing
read-only production data paths.  No Redis/TD write, repair, Rabbit consumer,
ACK, notification, or effect was enabled.  The probe used the thin providers
and the existing TD/Redis access conventions; it did not import the legacy
runtime service.

Commands were run from the isolated Core validation copy:

```text
examples/run_real_previous_day_stats.py
  --trade-date 2026-09-14
  --previous-trade-date 2026-09-11
  --symbols 600519,000001

examples/run_real_hot_plates.py
  --trade-date 2026-09-14

examples/run_real_previous_day_limit_pool.py
  --trade-date 2026-09-14
  --previous-trade-date 2026-09-11
```

Runtime: Python 3.12.3, `TZ=Asia/Shanghai`, `PYTHONHASHSEED=0`,
`LC_ALL=C.UTF-8`.  The three output files were created with exclusive-create
semantics under `/home/exedev/validation/core-real-probe-20260914-*`.

## Observed results

| Function | Source | Actual date | Rows | Redis scan | Core status | Availability evidence |
|---|---|---:|---:|---|---|---|
| `previous_day_stats` | TD `daily_kline` | 2026-09-11 | 2 | n/a | `UNAVAILABLE` | `available_at_unknown` |
| `hot_plates` | Redis `cache:hot_plates:2026-09-14` | 2026-09-14 | 50 | `True` | `UNAVAILABLE` | `available_at_unknown` |
| `previous_day_limit_pool` | Redis `cache:yest_limit_pool:2026-09-11` | 2026-09-11 | 40 | `True` | `UNAVAILABLE` | `available_at_unknown` |

Artifact SHA-256 values:

```text
core-real-probe-20260914-prev.json
20e78ee618330b196fdd584f4b35c3c9bbbce700f1e881649d71ad5ba51ebe7f

core-real-probe-20260914-hot.json
192547bda775244470edd8ba78035774b9a3831d12d63409120c335d656a6a3d

core-real-probe-20260914-limit.json
405c1ae9d0743b680bd64d4cafee19fe3a1f0ec5a33514f0480b720bc3fdcdb5
```

The TD query returned real `close`/`amount` rows and `volume=0` values, but
the meaning of that zero remains an observed/unknown source detail.  Redis
hot-plate and limit-pool payloads were real and scan-consistent; their metadata
did not prove historical publication time or verified field units.  Therefore
the TemporalDataGuard correctly refused to promote them to runtime-ready
data.  This is a successful fail-closed real-data check, not a provider
failure and not a Core replacement acceptance.
