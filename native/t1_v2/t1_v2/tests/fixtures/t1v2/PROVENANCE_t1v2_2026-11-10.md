# TD tick fixture provenance

Source: read-only TD `market_data1.stock_tick_v2`, stored upstream tick data, not raw upstream responses. Timestamps converted from Asia/Shanghai to Unix milliseconds; selected columns otherwise unchanged.

Time window: 2026-11-10 09:15:00–09:25:00 Asia/Shanghai; 3 rows total, one per query.

- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol = '301569' and ts = '2026-11-10 09:25:00'` — 1 row.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol = '301718' and ts = '2026-11-10 09:15:00'` — 1 row.
- `select ts,symbol,px_milli,pc_milli,amt_yuan,vol_units,bp1_milli,ap1_milli,bv1,av1,bv2,av2 from market_data1.stock_tick_v2 where symbol = '601512' and ts = '2026-11-10 09:15:00'` — 1 row.
