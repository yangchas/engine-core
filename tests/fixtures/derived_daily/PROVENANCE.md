# derived_daily fixture provenance

`real_kline_history.json.gz` is an extract of REAL production partitions of `~/core-data` taken on 2026-10-28 by
the C571 builder (read-only): the `daily_kline` partitions of every trade date from 2026-06-26 to 2026-10-27
(82 partitions) restricted to 74 symbols, plus the 2026-10-26 / 2026-10-27 `symbol_universe` rows of those symbols.

Selection (all rows are verbatim, nothing is edited or invented):
- the 10 strongest limit-up symbols of 2026-10-27 (pct_chg >= 9.8): 300504 301682 301479 301314 300539 300147 301361 301686 300877 301139
- every symbol with trade_status 0 (suspended) in the 2026-10-27 universe: 002717 002827 301468 600621 601326 605258 688496
- the 6 newest listings (first bar after 2026-09-20): 301686 301716 001246 301660 301569 301718
- 600621 601326 605258 688496: legacy_q2_final 2026-10-26 bars with amount 0.0 / volume 0.0 (suspended)
- 000016 601059 601198: symbols present in daily_kline but absent from the 2026-10-26 universe
- ~45 liquid symbols by stride over the sorted active universe, plus 000001 000002 600000 600519 300750 688981

Per partition the manifest keeps only `version, business_status, source, available_at, quality,
field_completeness`. 2026-10-26 is the real PROVISIONAL `legacy_q2_final` partition (no open, no turnover);
2026-10-27 is the real FORMAL `baostock>wencai>q2_final` partition.

The fake-clock check `examples/guardian_derived_trigger_check.py` seeds these rows into a TEMP catalog. Its upgrade
scenario re-registers the same real 2026-10-26 rows with `business_status=FORMAL` to exercise the trigger
mechanism only; that relabelled partition exists solely inside the temp catalog and is never written to ~/core-data.
