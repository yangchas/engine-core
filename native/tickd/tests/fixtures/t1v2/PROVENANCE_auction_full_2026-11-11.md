# C977 TD fixture

Source: read-only TD stored rows, not raw upstream responses. Timestamp is Unix milliseconds; `capture_tag` identifies the query. One last row per symbol in each half-open window.

- 0920: window [2026-11-11 09:15:00, 2026-11-11 09:20:03) Asia/Shanghai; rows: 5225.
  SQL: `SELECT last_row(ts),last_row(px_milli),last_row(pc_milli),last_row(amt_yuan),last_row(vol_units),last_row(bp1_milli),last_row(ap1_milli),last_row(bv1),last_row(av1),last_row(bv2),last_row(av2),symbol FROM market_data1.stock_tick_v2 WHERE ts >= '2026-11-11 09:15:00' AND ts < '2026-11-11 09:20:03' PARTITION BY symbol`
- 0924: window [2026-11-11 09:15:00, 2026-11-11 09:24:10) Asia/Shanghai; rows: 5225.
  SQL: `SELECT last_row(ts),last_row(px_milli),last_row(pc_milli),last_row(amt_yuan),last_row(vol_units),last_row(bp1_milli),last_row(ap1_milli),last_row(bv1),last_row(av1),last_row(bv2),last_row(av2),symbol FROM market_data1.stock_tick_v2 WHERE ts >= '2026-11-11 09:15:00' AND ts < '2026-11-11 09:24:10' PARTITION BY symbol`
- 0925: window [2026-11-11 09:15:00, 2026-11-11 09:25:06) Asia/Shanghai; rows: 5225.
  SQL: `SELECT last_row(ts),last_row(px_milli),last_row(pc_milli),last_row(amt_yuan),last_row(vol_units),last_row(bp1_milli),last_row(ap1_milli),last_row(bv1),last_row(av1),last_row(bv2),last_row(av2),symbol FROM market_data1.stock_tick_v2 WHERE ts >= '2026-11-11 09:15:00' AND ts < '2026-11-11 09:25:06' PARTITION BY symbol`
- cutoff_check: rows: 1; SQL: `SELECT ts,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2,symbol FROM market_data1.stock_tick_v2 WHERE symbol='301718' AND ts >= '2026-11-11 09:20:15' AND ts < '2026-11-11 09:20:20' ORDER BY ts`
- SHA-256: `f8658922512648d41e0164c3ec9c2e6e5f4347abe5a18c3a8b46a0fda3903506`
