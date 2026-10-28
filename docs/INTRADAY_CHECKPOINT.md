# Intraday checkpoint (`intraday_checkpoint`, C587)

Events `intraday_33/36/39/42/45` (handler `intraday_data`, gate `checkpoint_ready`) capture one market-wide snapshot per
3-minute checkpoint. Since C587 the snapshot comes from the live Redis `q2:{symbol}` hashes, so it also works after
09:45 (TD stores no tick after 09:45:59; C377 deleted the later events on a wrong premise).

## Source order and policy (schedule.json, `args.checkpoint`)
```
"checkpoint": {"sources": ["redis_q2", "td"], "tolerance_seconds": 60, "min_symbols": 4000,
               "td_until": "09:45:59", "redis_timeout_seconds": 5}
```
* `redis_q2` (primary): `SMEMBERS q2:active:YYYYMMDD` + pipelined `HGETALL q2:{symbol}` (read-only, no scan). A row belongs
  to the checkpoint iff its **own** `ts` (epoch ms) is on the trade date and in `[cutoff, cutoff + tolerance]`.
  Redis keeps only the latest hash: rows older than the window are stale for this cutoff, rows newer have been
  overwritten by a later tick. Neither is kept (they are counted in `quality.dropped`). Nothing is back-filled or relabelled.
* `td` (check / fallback): `stock_tick_v2` last tick per symbol inside the same window, only while the cutoff is
  `<= td_until`. A TD capture is written `degraded=true`, `source=td_stock_tick_v2_checkpoint`.
* Order is the order of `sources`; change it in the file, not in Python. `open_report_*` events have no `checkpoint` block
  and keep the old TD-only gate (their reports read TD themselves).
* Gate: ready when `>= min_symbols` rows are in the window (a 300-symbol sample rules out clear "not yet" polls cheaply).
  Reasons: `WINDOW_NOT_REACHED`, `checkpoint_below_threshold`, `CHECKPOINT_WINDOW_PASSED` (too many rows already
  overwritten: the checkpoint is lost, the instance ends `MISSED` at its deadline), `REDIS_UNREADABLE`.
* Deadline = cutoff + 3:59 (was +2:54; TD wrote its window ~2:45-2:53 late, so the old deadline lost the race).

## Handler
`intraday_data` with a `checkpoint` block: capture first, then the old `hot_plates` refresh. A `hot_plates` failure never
fails the event (a retry could no longer reproduce the window) - the event ends `degraded` and the detail names the error.
Exit codes of the capture: 0 ok / 10 degraded (TD) / 30 `CHECKPOINT_NOT_YET`, `SOURCE_UNREADABLE` (retryable) /
40 `CHECKPOINT_WINDOW_PASSED` / 64 config error (unknown `checkpoint` keys are rejected).

## Partition
`<core-data>/intraday_checkpoint/trade_date=D/source=redis_q2_checkpoint/sha256-*/{rows.jsonl.gz,manifest.json}`.
All checkpoints of a day are versions of the same date; the manifest field `checkpoint` ("09:33:00") identifies one;
`intraday_checkpoint.locate_checkpoint(catalog, day, "09:33:00")` finds it. `business_status=PROVISIONAL`
(a tick snapshot, never a final value), so `latest` is not advanced.

| column | unit / meaning |
|---|---|
| symbol, date | 6-digit code, trade date |
| ts | ISO-8601 +08:00 of the row's own feed timestamp (date evidence) |
| price, pre_close, high, low | yuan (Redis `px/pc/mx/mn` are milli-yuan) |
| pct_chg | percent = (px/pc-1)*100 |
| amount_yuan | yuan; volume_hands: hands (100 shares) |
| phase | Redis `ph` as text |

Manifest: `provider_attempts[{provider,result,elapsed_ms,reason,row_count,dropped}]`, `window`, `as_of` (= cutoff),
`captured_at` (Redis read time), `quality{coverage,expected_count (= q2:active size),missing,dropped,window_state}`,
`source_dates{row_ts_min,row_ts_max}`.

## Known limits
* Redis has no history: a checkpoint missed while the guardian was down stays missed (reported, not rebuilt).
* The feed's `ts` was measured at 30 s cadence with 0-80 s lag on 2026-10-28; `tolerance_seconds=60` covers one cadence.
* No TD table for this dataset yet (design/24 cold-storage rule): the catalog is the transitional ledger.
