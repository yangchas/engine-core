# Daily K-line in Core-owned TD (C567)

Status: implemented in source, **not deployed**. The production database `core_data` is created by the first
`td_sync_daily_kline` run after deployment (needs the rebuilt `guardiand`, see "Deployment").

## 1. Storage rule (design/24 section 2, user 2026-10-28)

Cold / big data goes to TD, hot data to Redis (with TTL), no temporary files. For `daily_kline`:

| role | where | note |
|---|---|---|
| versioned ledger (transitional) | local catalog `<core-data>/daily_kline/...` + `catalog.json` | written by every fetch; a fetch succeeds when the catalog write succeeds |
| query store, final authority | **Core TD database `core_data`** (own database, never `market_data1`) | filled by the sink; once TD and catalog reconcile, TD decides coverage |
| hot view | none (decision in section 8) | |

While TD lags behind the catalog, the guardian only warns and catches up; it never re-fetches what the catalog already
has. A day counts as complete when TD holds it and the read-back verified.

## 2. Database and tables

`show databases` on 2026-10-28: `information_schema, performance_schema, log, market_data1`. `core_data` does not collide.
`market_data1.daily_kline` (old system, 5211 child tables) is single-precision FLOAT with `volume`/`turnover` all 0; it is
only ever read (as an independent cross-check source), never written.

```sql
CREATE DATABASE IF NOT EXISTS core_data PRECISION 'us' KEEP 3650d DURATION 90d VGROUPS 2;
CREATE STABLE IF NOT EXISTS core_data.daily_kline_v1 (
  ts TIMESTAMP,                 -- trade date 00:00:00 Asia/Shanghai
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
  pct_chg DOUBLE,               -- percent (1.5044 = +1.5044 %), NOT basis points
  volume_shares BIGINT,         -- shares
  amount_yuan DOUBLE,           -- yuan
  turnover_pct DOUBLE,          -- percent, NULL when the source has none
  suspended BOOL,               -- NULL when the source does not say
  version VARCHAR(24)           -- catalog version (sha256-<16>) the row came from
) TAGS (symbol VARCHAR(8));     -- child table d_<symbol>
CREATE TABLE IF NOT EXISTS core_data.daily_kline_versions (
  ts TIMESTAMP,                 -- version available_at (unique per version)
  trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), rank_level INT, needs_upgrade BOOL,
  source VARCHAR(96), provider VARCHAR(32), degraded BOOL,
  quality VARCHAR(4000),        -- JSON: status, coverage, expected_count, expected_basis, partial, missing, dropped_rows, volume_factor
  row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), synced_at VARCHAR(40), schema_version INT);
```

* `KEEP 3650d` (about ten years), `DURATION 90d` (TD requires `keep >= 3 * duration`). One row per symbol per day: about
  5.2 k rows/day, about 1.3 M rows/year.
* `version` is the one column added to the planned list: it makes "which version are these rows" checkable on read-back.
* Version metadata is a separate normal table (planned: "单独的版本元数据表"); `quality` is a VARCHAR holding JSON because
  TD JSON is tag-only.
* Same `(day, symbol)` has exactly one row in `daily_kline_v1` (same `ts` in a child table overwrites); the history of
  versions lives in `daily_kline_versions`.

## 3. Column mapping (catalog row -> TD), the only place units change

| TD column | catalog row key | unit / rule |
|---|---|---|
| `ts` | partition `trade_date` | 00:00:00 +08:00, microseconds |
| tag `symbol` | `symbol` (or tail of `code`, e.g. `sz.000001`) | 6 chars, zero padded; repeated symbol: last row wins, counted in `quality.dropped_rows` |
| `open high low close pre_close` | same keys | yuan, DOUBLE (no float32) |
| `pct_chg` | `pct_chg` | percent, unchanged (C579: all readers use percent) |
| `volume_shares` | `volume` x factor | factor 1 when `field_units.volume` = shares (all Baostock/Wencai/new q2); 100 when `lots`, or when the unit is UNKNOWN and the provider is `q2_final`/`legacy_q2_final` (C579: those old partitions are lots); rounded to integer |
| `amount_yuan` | `amount_yuan` (fallback `amount`) | yuan |
| `turnover_pct` | `turnover_pct` (fallback `turnover`) | percent; NULL stays NULL, never 0 |
| `suspended` | `suspended` | bool only; anything else -> NULL |
| `version` | manifest `version` | |

Missing values are NULL. Columns present only in old partitions (`mismatch`, `universe_version`, `code`, `source`, `degraded`,
`tier`, `open_missing`, `quality`) have no reader (C579 evidence) and are not stored; `source`/`degraded` live in the version table.

## 4. Version choice and idempotent upsert

* Rank: `FORMAL (3) > PROVISIONAL (2) > DEGRADED (1)` (`derived_daily.STATUS_RANK`; unlabeled legacy = PROVISIONAL), then a
  version that needs no upgrade beats one that does (legacy unlabeled READY partitions), then the later `available_at`,
  then the later commit. Sample partitions (`sample_scope=symbols_file`) and empty versions are ignored.
* `sync_date(day)`: pick the best catalog version -> read the TD versions of that day -> if TD already holds a **higher**
  rank: `skipped_lower`; if TD holds the same version with matching row count (or an equal, newer one): `unchanged`
  (no write); otherwise **replace the day**: `DELETE ... WHERE ts = day` then insert all rows of the chosen version in
  chunks (stale symbols of a smaller FORMAL cannot survive), read back and verify, and only then write the version row.
  The version row is written last, so a crash in between leaves the day `INCOMPLETE` and the next run rewrites it.
* Re-running with the same input writes nothing; the version row `ts` is reused for the same version.

## 5. Read-back verification (tolerances)

Per day, computed independently from the catalog rows (not through the column map) and compared with TD aggregates:
row count, non-null counts of close/amount/volume/pct/open/turnover, `sum(volume_shares)` exactly (integers),
`sum(close)`, `sum(amount_yuan)`, `sum(pct_chg)` with relative tolerance `1e-9` (DOUBLE summation order only), plus a
deterministic sample of 120 symbols compared column by column (and their `version`). Any difference raises
`TdVerifyError` (deterministic, exit 40, version row not written). A wrong column, unit or a partial/merged write fails
(mutation tests in `tests/test_td_sink.py` and `tests/test_td_sink_real.py`).

## 6. Guardian integration

**Choice: a dedicated event `td_sync_daily_kline`, plus a best-effort post-fetch step and a plan action.** Reasons:
the event owns its retry ladder, backoff and single alert in `schedule.json` (profile `policy_05`: retry until
`late_until`, alert once when exhausted); a TD outage can never fail or delay a data fetch; each run is an idempotent
sweep over the lookback window, so it needs no knowledge of which fetch just finished and catches up by itself.

* Events (calendar `trading_day`, `cost: light`, handler `td_sync_daily_kline`, args may set `database`,
  `lookback_trading_days`): `td_sync_1515` (after the 15:05 close capture), `td_sync_1800` (after the first Baostock
  window), `td_sync_2015`, `td_sync_2335` (Baostock may land until 23:30).
* Post-step: `fetch_item` for `daily_kline` calls the same sync for the partition it just wrote when
  `td_sink.post_fetch_sync` is set (registry); failure only adds `td_sync_warning` to the result, the fetch stays
  successful (transitional rule: catalog write decides, TD lagging only warns).
* Startup catch-up (no manual backfill): `boot_reconcile` compares TD with the catalog for the last 5 trading days; a
  catalog version that TD lacks or holds only at lower quality becomes the plan action `SYNC_TD`
  (`id: daily_kline_td_sync`), executed by `boot_apply_plan`; the events sweep the last 10 catalog days, so history
  (e.g. 10-22 ... 10-27) is picked up at the first run without any manual step.
* TD unavailable: the handler exits 30 `TD_UNAVAILABLE` (retryable) -> guardian retries with backoff -> one alert when
  the ladder is exhausted; the next scheduled event / reconcile re-runs the full sweep and catches up.
* Config: registry item `daily_kline.td_sink` (database `core_data`, `self_check`, `post_fetch_sync`,
  `lookback_trading_days`). `CORE_TD_DATABASE` overrides the database name (tests / acceptance runs). Names starting with
  `market_data1`, `log`, `information_schema`, `performance_schema` are refused.

## 7. Self-check from TD (3b)

One interface, `guardian/daily_kline_coverage.py`:

* `CoverageSource.coverage(dates) -> {day: DayCoverage}`; implementations `TdCoverageSource` (primary) and
  `CatalogCoverageSource` (read-only fallback). `resolve_coverage` returns the per-day states plus the **origin**
  (`td`, `catalog_fallback`, `catalog` when TD is not configured in the process).
* States: `OK`, `MISSING` (no version / empty database / no schema), `INCOMPLETE` (rows != recorded row count),
  `LOW_QUALITY` (only PROVISIONAL/DEGRADED/PARTIAL), `UNKNOWN` (TD unreadable: represented by the fallback origin).
* `reconcile_plan` / `boot_reconcile` use it through `daily_kline_day_state`; `_daily_kline_candidates` still yields the
  calendar window (derived datasets share it), the coverage judgement is applied to exactly those days.

| TD says | catalog has | plan |
|---|---|---|
| OK | anything (even nothing) | nothing |
| MISSING / INCOMPLETE | usable version | `SYNC_TD` (no fetch); fetch too only if that catalog version itself needs upgrade |
| MISSING / INCOMPLETE | nothing | `FETCH_NOW` (missing day) |
| LOW_QUALITY | better version | `SYNC_TD` |
| LOW_QUALITY | nothing better | `FETCH_NOW` with `upgrade_daily_kline` (existing attempt budget / quiet windows) |
| unreadable | complete | nothing; origin `catalog_fallback`, one alert `daily_kline_td_unreadable` per outage (re-armed by the first successful read or sync) |

An unreadable TD never turns into "missing": no mass re-fetch. A fetch is complete when the catalog write succeeded **and**
TD verified the day; until then the next `boot_reconcile` shows `SYNC_TD` rather than re-fetching.

## 8. Redis hot view for the latest day

**Decision: none in this card.** Daily bars are cold data (written once per day after 15:05, about 5.2 k rows); every
consumer found by C579 reads the catalog or TD, none reads a Redis key for yesterday's bar, and live prices already come
from the q2 snapshot. If a hot consumer appears, the view is: key `core:daily_kline:latest:v1` (hash `symbol` -> JSON of
the TD row + `version`), TTL 36 h, written by the sink right after a verified sync of the newest trade date (so the
sink is the only writer). Not implemented until a consumer exists.

## 9. Deployment notes (not done here)

1. Rebuild `guardiand` (the handler whitelist in `native/guardiand/main.go` gained `td_sync_daily_kline`) and regenerate
   the config (`migrate` + `scripts/make_config.py`). An old binary rejects the new `schedule.json` events.
2. Production database creation happens at the first `td_sync_*` run (`CREATE ... IF NOT EXISTS`); before that
   `boot_reconcile` reports `MISSING` for every day and plans `SYNC_TD`, which creates it.
3. `TD_CONFIG` is already in the handler `env_allow`.

## 10. Known limits

* TD DELETE + INSERT per day is not one transaction; the version row is written last and read-back fixes it (INCOMPLETE).
* `boot_data_health` (`stale_items`) still reports catalog freshness; only the reconcile plan is TD-based.
* Old catalog partitions without `business_status` rank as PROVISIONAL unless their manifest says no upgrade is needed.
