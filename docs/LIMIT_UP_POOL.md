# Yesterday's limit-up pool (D-1), three-tier ladder (C580)

Dataset `limit_up_pool`, partition `trade_date=D-1` (the described day, never the fetch day), key `(symbol, date)`.
Single writer: `src/engine_core/guardian/limit_pool_d1.py`; pure core `data_pipeline/limit_pool.py`; TD sink
`data_pipeline/limit_pool_td.py`; spec `data_pipeline/dataset_specs/limit_up_pool.json` (checker: `examples/check_dataset_contract.py`).

## 1. Sources and quality (order is configuration, not code)

| rung | provider (= `source=` dir) | business_status | what it is |
|---|---|---|---|
| 1 `kaipan` | `kaipan_getHisBans` | FORMAL | vendor ladder `getHisBans(date, ban=level)` level by level until `errcode 1020`; excludes ST (vendor); all 23 positional fields kept |
| 2 `wencai` | `wencai` | PROVISIONAL (degraded=true: not the primary) | query `"{date}涨停;连板天数;去除st"`, members equal Kaipan on both checked days; extra columns (seal amount, open count, ...) |
| 3 `td_derived` | `td_derived` | DEGRADED | inferred from read-only TD `market_data1.daily_kline` (close, high, pct_chg; volume/turnover are 0 there): `pct_chg >= board_limit - 0.6 and close == high`; board limit by listing board only (688/689/30: 20, 8/4/92: 30, other: 10; there is no ST 5% tier); ST removed by name from the newest `symbol_universe` on or before the day, if no universe exists the manifest says `st_filter: UNAVAILABLE` (nothing is assumed); no seal evidence |

A lower rung never replaces a better version: the rung returns `ALREADY_SATISFIED` (exit 20) when a contract-v2 version of equal or
better quality exists; `ArtifactCatalog.locate` and `latest` for this dataset prefer quality (FORMAL > PROVISIONAL > DEGRADED), equal quality
the newest. Legacy 4-column partitions (before C580, `schema_version` 1) never satisfy a rung and rank as unlabeled = PROVISIONAL.

## 2. Ladder configuration (`schedule.json`, event `limit_pool_d1`, profile `limit_pool_d1`)

* event: trading days, `at 00:40:00`, `data_date_offset -1`, `deadline = late_until = 15:00:00`, `on_deadline missed`, `catchup run_in_window`,
  handler `kaipan_fetch` with `args.dataset = limit_up_pool` (the bridge routes it to `run_pool_tier`; the other `kaipan_fetch` event
  `kaipan_refresh_0850` keeps reasons / hot plates / members and re-checks the pool with one ladder pass through `run_data_jobs --job limit_up_pool`).
* rung 1 kaipan: 34 attempts, `backoff 15m*1` (00:40, 00:55, ..., 08:55), timeout 5m, `args.retry_until 08:50:00`;
  not published (`errcode 1020` at level 1) = exit 30 `KAIPAN_NOT_READY` (retry); once the request clock is past `retry_until`
  the same condition becomes exit 40 `TIER_NO_DATA` and the ladder moves on at once (a guardian started at 10:00 with Kaipan down does not wait).
* rung 2 wencai: 2 attempts, `backoff 30m*1` (pre-market cooldown in the 30 minute range), timeout 10m, one request budget 3, cookie file path in args.
* rung 3 td_derived: 2 attempts, `backoff 5m*1`, timeout 5m.
* `TIER_NO_DATA` / `TIER_DATE_MISMATCH` advance directly; retryable codes (30) are retried inside the rung with its backoff; deterministic
  non-`TIER_*` failures advance and, with `alert_on_fallback`, produce one alert when a later rung succeeds; every rung failed = `FAILED_FINAL`
  and one alert. `chain` in the rung args must equal the rung order (test `test_chain_order_in_config_matches_rung_order`).
* Only downgrades written in the ladder happen. Reordering the ladder in the config changes which source runs first (fake-clock scenario J).

## 3. Kaipan D-1 availability window and what happens outside it

* The vendor publishes day D only on the following calendar day: design/FACTS say from about 00:30 (D+1). Verified in this card: a request
  for the current day at 10:00 CST answers `{"errcode": "1020", "errmsg": "参数出错"}` (fixture `his_bans_not_ready_20261028.json`), and the
  live partition of 2026-10-27 exists with `captured_at 2026-10-28T00:30:33Z` (= 08:30 CST). The 00:30 CST lower bound itself was not re-measured
  (no probe at that hour); polling starts at 00:40 and the ledger records the first successful poll, which is the operational measurement.
* Inside the window (00:40 .. 15:00 of D): polled every 15 minutes until 08:50, then the ladder continues to WenCai and TD; a guardian started
  late backfills immediately (scenarios B, C, I).
* Beyond `late_until` (15:00): a guardian started later marks the instance `MISSED` explicitly (state + alert), fetches nothing and writes nothing
  (scenario H). No partition is ever fabricated, copied from another day or back-filled by hand. Later recovery is a separate, visible action: the
  generic `fetch_item` / `boot_reconcile` path calls `run_data_jobs --job limit_up_pool --execute`, which makes one pass over the same configured
  rungs (`run_ladder_once`, no waiting); this path is covered by unit tests only, its guardian wiring was not exercised in this card.
* Kaipan keeps its history, but `getHisPlates` keeps only about 3 trading days and `getBanReasons` returns only the latest reason per stock
  (they are other datasets, C581); the pool itself was retrievable for 2026-10-19/23/26/27 in this card.

## 4. Fields (rows) and units

Common (all sources, required): `symbol` (6 digits), `date` (= partition day), `trade_date` (legacy alias of `date`), `name`, `lb_days`
(consecutive limit-up days up to and including `date`; Kaipan: the record's own field [15], the ladder level only when [15] is empty; WenCai: 连续涨停天数; TD: derived), `in_legacy_pool` (always true).

Shared values: `price` (yuan, close of `date`, Kaipan [21] / TD), `pct_chg` (percent, Kaipan [22] / TD). Verified against independent TD bars:
0 of 77 (10-23) and 0 of 55 (10-26) differ by more than 0.006 yuan / 0.06 percent.

Kaipan (all 23 positional fields; the card said 16, the real record is 23 wide, verified on 2026-10-19/23/26/27): `limit_time_epoch` [4] unix seconds
(first limit-up time, 2026-10-27 sample 09:25:00 CST), `seal_time_text` [3] (empty in every sample, stored null), `reason_category` [5] short reason /
plate category (e.g. 商业航天), `reason_plates_text` [12] plate text '、'-separated, `plate_code` [19] plate index code, and raw positions `kp_02, kp_06 ..
kp_11, kp_13 .. kp_18, kp_20` stored verbatim (meaning and unit NOT verified; `kp_10 = kp_8 - kp_9` in all samples; `kp_15` is the vendor's consecutive-days
number and feeds `lb_days`; it differs from the ladder level for 2 of 82 records on 2026-10-23, counted in `provider_details.lb_days_differs_from_ladder_level`). Names of verified positions are ours; values are unchanged.

WenCai (`wc_*`, from the `[YYYYMMDD]` columns of the requested day): `wc_limit_type`, `wc_streak_text`, `wc_first_limit_time`, `wc_last_limit_time` (HH:MM:SS),
`wc_open_count`, `wc_seal_volume_shares`, `wc_seal_amount_yuan`, `wc_seal_vol_ratio` and `wc_seal_float_ratio` (as reported, percent assumed, unverified),
`wc_float_mktcap_yuan`, `wc_reason_category`. TD derived: `limit_pct` (10/20/30).

## 5. Manifest / contract

Required manifest fields of design/24 section 4 plus `provider_details` (counts of dropped records, st_filter, rule). `provider_chain` is the configured
ladder order; `provider_attempts` the attempts of this call. Date evidence (`source_dates`): Kaipan = the date string the vendor echoes in `info`
(`info[1]`), WenCai = the `[YYYYMMDD]` suffix of the columns, TD = the bar date; a missing or different date rejects the response (`TIER_DATE_MISMATCH`).
`as_of = D 15:00+08:00`, `available_at` = commit time (the checker also requires, for Kaipan, `available_at >= D+1 00:30`: check A10b).
Coverage: `expected_basis = pool_members`, `expected_count` = members the source reported (Kaipan: records seen over all levels; WenCai: `row_count` of the
response; TD: derived rows), `missing`, `coverage`, `partial`, `dropped_*`. The universe is not the denominator.

## 6. TD storage (cold data; the catalog partition is the transitional versioned ledger)

Database: that of `daily_kline` (`core_data`, overridable by `CORE_TD_DATABASE`; `core_dev_*` in tests; `market_data1` refused).
`limit_up_pool_v1` super table `(ts TIMESTAMP, <38 data columns>, version VARCHAR(24)) TAGS (symbol VARCHAR(8))`, child `p_<symbol>`, `ts` = D-1 00:00 +08:00;
every catalog row key maps to the column of the same name (str -> VARCHAR(n), int -> BIGINT, float -> DOUBLE, bool -> BOOL, missing -> NULL; `date`, `trade_date`
and `symbol` are `ts` and the tag). `limit_up_pool_versions` (same layout as `daily_kline_versions`, without `needs_upgrade`) holds one row per version
(status, provider, quality JSON, row count, sha256). Sync = replace the day, read EVERY cell back and compare, then write the version row last; a day with a
lower-ranked version never replaces a higher one in TD. Immediately after a successful catalog commit the runner syncs (a TD failure only adds
`td_sync` warning to the result: the catalog write decides success); the scheduled `td_sync_*` events also sweep pool partitions, so TD catches up by itself.
Hot copy (Redis): none; the pool is read once a day per consumer, from the catalog/TD.

## 7. Known limits

* TD derived pool is inferred (no seal evidence); it differs from the vendor pool only by listing/ST/suspension cases listed in the C580 report.
* The legacy TD table has no row for very new listings and Beijing 92xxxx codes outside its universe (10-23: 5 of 82 members).
* On 2026-10-23 two Kaipan records (002724, 603278) sit on ladder level 5 while their own field [15] and WenCai and TD all say 6 (six real consecutive limit-up closes): `lb_days` therefore uses [15], not the page level.
* WenCai: one page (<= 100 rows) per request, about 4-5 s, 1 request per date here; cooldown 60 s doubling to 300 s; cookie path only in config, never logged.
* Kaipan: 10 s between requests (existing provider), 6 requests / about 51 s for a 5-level day.
