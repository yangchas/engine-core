# C976 TD fixture

TD stored rows, not raw upstream responses; Asia/Shanghai timestamps converted to Unix milliseconds; other fields unchanged.

- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301569' and ts = '2026-11-11 09:25:00'` — 1 row.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301718' and ts = '2026-11-11 09:20:00'` — 1 row.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='600835' and ts = '2026-11-11 09:15:00'` — 1 row.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol='301718' and ts > '2026-11-11 09:25:06' and ts < '2026-11-11 09:31:00' limit 1` — 1 row.

Captured 2026-11-11 Asia/Shanghai; SHA-256 `41bd3e603d4ecad12b591b34d0a1ce03e0b8cdb82a5650aa036666cb518b9abc`. Pre-cutoff match, pre-cutoff fallback, missing book, and post-cutoff tick respectively. Test order is a specification arrangement, not observed arrival order.
