# TD read-only connectivity dry-run

- Run date: 2026-09-07 (Asia/Shanghai)
- Remote host: `cobra-ion`
- Trade date queried: `2026-09-04`
- Tool: existing `/home/exedev/audit/tools/ground_truth_capture.py td-dry-run`
- Database: `market_data1`
- Runtime: existing engine-next server venv (Python 3.12)
- Result: `PASS`
- Write isolation: Redis `0`, TD `0`, notification `0`, network repair `0`

## Read-only export summary

| Dataset | Rows | Symbols | Time range |
|---|---:|---:|---|
| `auction_snapshot_v2` / 0920 | 5,215 | 5,215 | 09:20:03.440 |
| `auction_snapshot_v2` / 0924 | 5,215 | 5,215 | 09:24:10.042 |
| `auction_snapshot_v2` / 0925 | 5,215 | 5,215 | 09:25:06.052 |
| `auction_summary_v2` | 3 | — | 09:20:03.440–09:25:06.052 |
| `stock_tick_v2` / auction | 223,789 | 5,215 | 09:15:00.000–09:25:04.000 |
| `stock_tick_v2` / open | 304,725 | 5,207 | 09:30:00.000–09:32:59.000 |

The complete dry-run manifest recorded the per-artifact SHA-256 values and query text. The large temporary JSONL exports were removed after verification to avoid consuming production disk; this document is the retained summary evidence. This dry-run is not formal ground truth and does not establish Rabbit arrival/batch order or historical `available_at` semantics.
