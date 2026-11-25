# C958 TD fixture

TD stored rows, not raw upstream responses; Asia/Shanghai timestamps converted to Unix milliseconds; other fields unchanged.

- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301569' and ts >= '2026-11-09 09:25:00' and ts <= '2026-11-09 09:25:06' limit 1` — 1 rows.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301569' and ts > '2026-11-10 09:25:06' and ts < '2026-11-10 09:31:00' limit 1` — 1 rows.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301569' and ts >= '2026-11-10 09:15:00' and ts < '2026-11-10 09:25:00' limit 1` — 1 rows.

Captured 2026-11-10 Asia/Shanghai; SHA-256 `0552d7e30d8af8fb392054703715d032be42b8f174944a9725340b44e519fa9d`. Batch order and logical clock in self-test are specification arrangements, not observed arrival distributions.
