# daily_indicators / daily_report_features (C571)

Derived daily datasets, algorithm id `derived_v1`. Contract: `fw/design/24-DATASET-CONTRACT.md`; machine spec:
`src/engine_core/data_pipeline/dataset_specs/daily_indicators.json` and `daily_report_features.json`; checker:
`examples/check_dataset_contract.py --dataset X --date D [--root R]`.

## 1 Code layout (decoupling)

| piece | file | rule |
|---|---|---|
| pure builders | `src/engine_core/data_pipeline/derived_daily.py` | `build_daily_indicators(request)`, `build_daily_report_features(request)` return `Partition(rows, manifest)`. No env, no disk, no Redis, no clock, no guardian import (enforced by `tests/test_derived_daily.py`). |
| formulas | `src/engine_core/daily_history.py` (`calculate_daily_indicators`, `build_daily_report_features(rows, trade_date)`) | unchanged legacy formulas = regression baseline |
| adapters | `daily_history.materialize_daily_indicators / materialize_daily_report_features`, `examples/build_daily_derived.py`, `examples/build_daily_report_features.py`, `examples/backfill_daily_history.py` | read the catalog, build the request, call the core, commit with `source=derived_v1`, exit code |
| trigger | `guardian/datasets.registry.json` (`derive`, `fetch_job`), `guardian/registry.derived_needs_build`, `guardian/reconcile_plan.py`, `data_pipeline/ops.run_derived_daily` | see section 5 |

Request: `trade_date, as_of (tz-aware), fetched_at?, window{max_days=120}, min_upstream_status, daily_kline[ {trade_date, version, business_status, source, available_at, rows} ], symbol_universe{trade_date, version, rows}, daily_indicators{trade_date, version, business_status}` (features only).
Exit codes of `build_daily_derived.py`: 0 ok (PROVISIONAL results are ok), 20 upstream not ready, 30 retryable I/O, 40 deterministic.

## 2 Data structure

Primary key `(symbol, date)`; one partition per `trade_date`, rows sorted by `symbol`, `date` == partition trade_date, `symbol` 6-digit string.
Upstream unit facts (verified on the real 2026-10-27 FORMAL partition, not from docs): close/high/low/open in yuan, unadjusted (`adjustflag=3`), `pct_chg` in percent points (000001: 11.47/11.30-1 = 1.5044 stored as 1.5044; `docs/DAILY_KLINE.md` still says bp, it is wrong for the persisted rows), `turnover_pct` percent, `amount_yuan` yuan, `volume` shares.
Adjustment: unadjusted prices, same as daily_kline. MA/bias across an ex-dividend date therefore contain the price gap (known limit, not corrected).

### daily_indicators
| column | meaning | unit | window / null rule |
|---|---|---|---|
| ma5 / ma10 / ma20 | mean of the last n closes | yuan | needs n bars (rows of that symbol, none with null close), else null |
| bias_20 | (close - ma20) / ma20 * 100 using the 2-dp **rounded** ma20 | percent | null when ma20 null. Rounding ma20 first adds up to 0.5/ma20*(1+bias) percentage points (0.2 pp for a 2.5-yuan stock); kept for legacy regression |
| rsi_6 | Wilder RSI(6) over the whole retained history | 0-100 | needs >= 7 closes |
| change_pct_5d | (close / close 5 bars ago - 1) * 100 | percent | needs 6 bars |
| avg_turnover_5d | mean turnover_pct of the last 5 bars | percent | needs 5 bars with turnover; null if any is missing |
| limit_up_days_5 | bars with pct_chg > 9.8 (main) / 19.8 (300,301,688,689) / 29.8 (8, 92) in the last 5 | days 0..5 | needs 5 bars with pct_chg; ST/name not considered (legacy heuristic, UNVERIFIED for ST) |
| vol_ratio | avg_turnover_5d / (mean turnover of bars -20..-5 + 0.1) | dimensionless | needs 20 bars with turnover |
Insufficient history or missing inputs give **null with `quality.field_completeness` COMPLETE/PARTIAL/MISSING per column**, never 0. `avg_turnover_5d` and `vol_ratio` are null for every partition whose window contains 2026-10-26: that day is a PROVISIONAL `legacy_q2_final` bar without a turnover field. They fill in when 10-26 becomes FORMAL (the spec marks both `all_null_ok` with this reason; the checker prints a WARN).

### daily_report_features
Rows: one per universe symbol (columns `feature_type="daily_report_features_v1"`, `trade_date`, `last_history_date`, `streak` (consecutive limit-up closes, board-specific 10/20/30% limit, price compared at 2 dp), `position_20d` ((close-min)/(max-min) of the last 20 closes, null when flat), `position_close_days` (0..20), `range20_low/high/last_close` (yuan, last 20 closes, null below 20), `prev_day_amount_yuan` (yuan, the partition day's amount), `amount_history_64` (yuan, last 64 trading days, null unless complete)) plus **one sentinel row `symbol="__MARKET__"`** carrying `opening_history_72` (72-session market open median/up-share baseline) and `amount_history_dates_64`. The sentinel is consumed by `live_session` / `daily_pipeline`; it is the only non-6-digit key and the spec declares it (`sentinel_symbols`, at most one, checked by B1a). PIT: only daily_kline partitions with `available_at <= as_of` and `trade_date <= D` are read; the partition day itself is included (feature "as of the close of D"), never a later day.
A zero-amount/zero-volume bar is a suspended bar (legacy_q2_final writes 0.0 for them): its amount is set to null in the derived input (contract section 3, null not 0) and counted in `quality.no_trade_amounts_nullified`.

## 3 Inputs, versions, quality inheritance

- Inputs are persisted partitions only: `daily_kline` (best-quality version of each of the last 120 trading-day partitions <= D), `symbol_universe` D (coverage denominator), and for features also `daily_indicators` D.
- Manifest `inputs` (a mapping, because `ArtifactCatalog.commit` hashes only a mapping into the version): `daily_kline{trade_date,version,business_status,source,history_days,history_digest}`, `symbol_universe{...}`, `daily_indicators{...}`. `history_digest` is the hash of every (day, version) pair in the window, so **a changed upstream version, even with identical output rows, produces a new output version**. `input_refs` lists every consumed partition as `[{dataset,trade_date,version,business_status}]`.
- `business_status` = lowest status among the partition day's own inputs (its daily_kline; for features also its daily_indicators). History days older than D are recorded (`quality.history_non_formal_days`) but do not cap the status: 2026-10-22/23/26 are PROVISIONAL and have exhausted their upgrade budget, so inheriting them would keep every later partition PROVISIONAL forever. Unlabelled upstream counts as PROVISIONAL. Minimum accepted upstream status is configured per item (`derive.min_upstream_status`, currently PROVISIONAL); DEGRADED upstream is refused (`UPSTREAM_BELOW_MIN_QUALITY`).
- Storage: `<root>/<dataset>/trade_date=D/source=derived_v1/sha256-xxxxxxxxxxxxxxxx/{rows.jsonl.gz,manifest.json}`, `provider=derived_v1`, `provider_chain=[]`, one `provider_attempts` entry. PROVISIONAL never advances `latest`; a FORMAL recompute is appended as a newer version and `locate()` returns it. (Limit: `ArtifactCatalog._can_advance_latest` refuses to move `latest` within the same trade_date for non-kline datasets, so a FORMAL-over-FORMAL recompute leaves `latest` on the older version; readers use `locate()`. Fix belongs to storage.py, not done here.)
- Manifest quality: coverage, expected_basis=`universe_active`, expected_count, missing, suspended_in_universe, field_completeness, partial, dropped_outside_universe, dropped_duplicates, no_trade_amounts_nullified, history_trade_dates, history_non_formal_days. History import of days older than the first symbol_universe partition uses `expected_basis=daily_kline_symbols` (explicit in the manifest).

## 4 TD column mapping (proposal; TD sink is wired after C567)

| partition column | TD type | note |
|---|---|---|
| ts = trade_date 15:00+08:00 | TIMESTAMP | primary timestamp of a daily row |
| symbol | BINARY(6) TAG | |
| ma5 ma10 ma20 bias_20 rsi_6 change_pct_5d avg_turnover_5d vol_ratio | DOUBLE | NULL when null |
| limit_up_days_5, position_close_days, streak | SMALLINT | |
| position_20d, range20_low/high/last_close, prev_day_amount_yuan | DOUBLE | |
| amount_history_64, opening_history_72, amount_history_dates_64 | not in TD (JSON in the catalog / Redis hot view) | |
| version metadata (version, business_status, inputs digest, as_of) | separate version table | catalog stays authoritative |

## 5 Trigger design (decision: gate on upstream, never on a clock)

Evidence (guardian ledger `~/core-data/guardian-native/state/ledger`, read-only):
- 2026-10-26: `postmarket_publish` MISSED at 20:30 (17:40 `QUEUED_HEAVY`, heavy slot occupied). `runtime_reconcile_2100` at 21:00 created `fetch_item:daily_kline` and, waiting on it, `fetch_item:daily_indicators/daily_report_features/ladder_daily/stock_day_facts` (seq 529-532, state DEFERRED, `wait_for fetch_item:daily_kline`). the three daily_kline attempts failed (FETCH_EXIT_10) and the item went FAILED_FINAL at 21:10:31 (seq 548) although `legacy_q2_final` had landed a PROVISIONAL partition at 21:02:30. The four derived instances never left DEFERRED (no later record). The derived 10-26 partitions that exist carry available_at 2026-10-27T08:30+08:00, i.e. they come from the 08:30 premarket prep (`prep_trade_day`, which materializes D-1), not from any post-close trigger. The PROVISIONAL 10-26 kline was never upgraded (`guardian-upgrade-attempts.json`: 3 attempts, exhausted), so nothing re-derived it.
- 2026-10-27: daily_kline FORMAL landed 18:26:24 (gate poll 556); `postmarket_publish` then ran only the daily_kline phase (`core_data_daily.py --phase postmarket`, ATTEMPT_END exit 0 after 1.2 s: far too short to compute indicators) which never contained the two derived datasets. `runtime_reconcile_2100` (21:00) and `boot_reconcile` (21:34) logged no `fetch_item` for them although the 21:34 health file marks both OVERDUE. Reproduction with the real plan code on the real catalog and `load5=2.94` (the load recorded in `guardian-upgrade-attempts.json`, `load_defer_last_window_id 2026-10-27:POST_MARKET`): `daily_kline 2026-10-22` is `DEFER load5>threshold` and `daily_indicators/daily_report_features 2026-10-27` become `DEFER blocked_by:daily_kline`, because dependencies were matched by item id, not by partition: an upgrade of another day blocked today's derived work. With low load the same plan is FETCH_NOW. (The daemon's stdout is not in the ledger, so the exact load at 21:00/21:34 is inferred, not read.)
- Upgrade gap: `derived_daily_kline_input_stale` existed but only looked at the target day (today / yesterday), only for manifests that recorded `inputs`, and nothing ran after a daily_kline upgrade except the next reconcile; an upgraded older day was never re-derived.

Decision (option a, gate + reconcile backfill; option b was rejected because the daemon only knows a fixed handler list, a new post-success handler needs Go changes):
1. Gate: a derived partition D is due when `daily_kline D` is present (coverage/availability) at `>= derive.min_upstream_status` and `symbol_universe D` is present, and the derived partition is missing or one of its recorded input versions is no longer the best catalogued version (`registry.derived_needs_build`). No time of day enters the decision (fake-clock S0).
2. Immediately: `ops.run_daily_phase("postmarket", write=True)` runs both builders right after the gated daily_kline step, i.e. in the same `postmarket_publish` / `baostock_*` events that published the upstream (these events are `daily_kline_ready`-gated and already retried until 23:30).
3. Backfill / upgrade: `reconcile_plan` checks the last `DAILY_KLINE_BACKFILL_DAYS` (5) trading days oldest first for each derived item, so a late-started guardian backfills (`runtime_reconcile_*` run every 1-2 h, plus `boot_reconcile`), and a daily_kline PROVISIONAL->FORMAL upgrade makes `input_refs` stale -> recompute as a new version. `build_daily_derived.py --cascade` rebuilds later days whose recorded history changed in the same run.
4. A dependency blocks only when it is due for the same partition and is a real missing-data fetch (not an upgrade). Derived cost is `medium` (10 s measured), so load deferral does not apply.

Open (needs Go, not in scope): a `fetch_item` waiting on a FAILED_FINAL dependency stays DEFERRED forever (ledger 10-26 seq 529-532); with the new gating the derived items no longer wait on daily_kline instances.

## 6 Operation
```
python examples/build_daily_derived.py --dataset daily_indicators --date 2026-10-27 --artifact-root ~/core-data   # normally run by guardian
python examples/check_dataset_contract.py --dataset daily_report_features --date 2026-10-27
python examples/guardian_derived_trigger_check.py [--mutation no_input_refs|fixed_clock_trigger|bad_window]
```
Tests: `tests/test_derived_daily.py` (pure, values vs independent recomputation, contract mutations, stale detection, fake-clock chain and its 3 mutations), fixture provenance `tests/fixtures/derived_daily/PROVENANCE.md`.
Known limits: bias_20 from rounded ma20; limit_up_days_5 ignores ST; unadjusted prices; avg_turnover_5d/vol_ratio null while a PROVISIONAL no-turnover bar is in the window; features/indicators for days before the first symbol_universe partition (2026-10-15) only exist from the history import path with `expected_basis=daily_kline_symbols`.
