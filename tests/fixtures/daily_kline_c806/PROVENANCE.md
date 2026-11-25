C806 Baostock capture, 2026-11-08 Asia/Shanghai. Original source directory:
`/home/exedev/validation/acceptance/0ceed14/c806/`; capture.py recorded the
`query_history_k_data_plus` and `query_stock_basic` responses. Each JSONL
record is copied byte-for-byte for symbols 688796, 688797 and 301139.
688797 is a simulated already-present control; C806 did not capture a stock
known to have been successfully filled in the investigated run.

| File | Original SHA-256 | Selected records SHA-256 |
| --- | --- | --- |
| history.jsonl | 34f89dcb597899a97f74c057377585a786041a872163c55d50f96475b9c5bd7b | a912f21f8262bde695ea1127add989d92b9b359d2a8fac394a97f2474ada7ad3 |
| basic.jsonl | 182e7b19db1ada0ae59b9346067431054dcce3f974a3689ff8dc63853c3a7dc1 | e55e0c9686db5c3c6de11d78636bb76a0f7d5609173853ca1580691fdce6b082 |
