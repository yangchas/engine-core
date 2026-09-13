# M2 provider chain read-only audit — 2026-09-13

## Scope

This audit inspected the current cobra-ion release without changing the
production services, Redis keys, TDengine tables, RabbitMQ consumers, ACK
behavior, or notification paths.

- release: `/home/exedev/services/engine-next/releases/20260903_e272842`
- release commit: `e272842c8f490f55a1b017badb71e71904ce008e`
- services at observation time: `engine-next=active`, `t1-v2-live=active`
- observation time: 2026-09-13 Asia/Shanghai
- side-effect boundary: `systemctl show`, source inspection, Redis
  `TYPE/HLEN/HSCAN/GET` only

## Verified producer → writer → reader path

### Previous-day limit pool and hot plates

1. `engine_next/connectors/kaipan_connector.py` constructs the legacy
   `ai.API.StockAnalyzer.StockAnalyzer` lazily.
2. `StockAnalyzer` calls the existing `pykaipan` API through `_call_api`.
   `get_history_bans_pool()` requests `getHisBans` for each board level from
   `1` through `max_ban`, then normalizes the legacy positional record into
   `code/name/lb_days/plate/seal_time/turnover/close_pct`.
3. `engine_next/runtime/intraday_data_hub.py` owns the cache-writing path:
   `fetch_hot_plates`, `fetch_hot_rank`, `fetch_yest_limit_pool` and
   `fetch_limit_truth` write their date-bucketed Redis projections and JSON
   metadata.  These methods are not read-only providers.
4. `engine_next/app_main.py` reads the projections for runtime readiness and
   reporting.  The prior-limit reader additionally uses
   `runtime/prior_limit_cache_contract.py` to validate trade date, source,
   row count and payload hash before accepting a cache.

## Redis observations

The following date partitions were read with Redis `TYPE`, `HLEN`, `HSCAN`
and `GET`; no write command was issued.

| date | hot_plates | hot_rank | yest_limit_pool | limit_truth |
|---|---:|---:|---:|---:|
| 2026-09-08 | 50 | 100 | 73 | 73 |
| 2026-09-09 | 50 | 100 | 48 | absent |
| 2026-09-10 | 50 | 100 | 35 | absent |
| 2026-09-11 | 50 | 100 | absent | absent |

All present projections were Redis hashes and the scanned row counts matched
`HLEN` for this bounded observation.  The date fields in sampled JSON rows
matched the requested partition.

The current release metadata contains update/attempt timestamps and source
labels, for example `updated_at`, `updated_at_ts`, `last_attempt_at` and
`last_attempt_at_ts`.  It does **not** contain a verified
`schema_version`, `available_at_ms`, or field-unit contract for these caches.
The yest-limit metadata does contain a payload hash in the observed release;
the hot-plate and hot-rank metadata do not provide equivalent availability or
unit evidence.

## Acceptance classification

The real reads prove the physical cache path and the legacy reader contract,
but they do not prove historical knowledge availability.  The current core
providers therefore correctly return:

```text
hot_plates:       UNAVAILABLE (available_at_unknown)
yest_limit_pool:  UNAVAILABLE (available_at_unknown, turnover_unit_unknown)
```

This is a fail-closed result, not a provider connectivity failure.  M2 remains
blocked by:

```text
available_at_unknown
turnover_unit_unknown
producer_runtime_not_deployed
```

No producer metadata patch is deployed and no Redis/TD data was modified by
this audit.

## Next action

Do not add another provider hierarchy or repeat this inventory.  Continue the
minimal Gate B auction fact shadow using the already verified 600519
`auction_snapshot_v2` fixture/TD evidence.  Re-open M2 only when a controlled
producer contract is deployed and a new read-only run observes verified
`available_at_ms` and field units in the actual cache metadata.
