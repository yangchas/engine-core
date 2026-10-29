# Hot plates

Hot plates are Kaipan's board-strength view. The daily history job fetches the last
`N` trading sessions strictly before its `--date` (default `N=3`, allowed `1..3`);
on trading day D this includes D-1. The configured window fits the observed Kaipan
history retention of about three sessions. The capture CLI refuses an older history
date without calling Kaipan and records `historical_outside_retention` as a
`late_policy=gap_only` row. `Day` from each response page must match the requested
source date. A mismatch or failed history fetch becomes a GAP; no Wencai replacement
exists for hot plates. Guardian schedules `hot_plates_history_0815` at 08:15 with
an 08:30 deadline and one bounded 300-second attempt. A missed window records one GAP for
each requested source date when the next hot-plate capture or 15:15 reconciliation
runs; reconciliation makes no provider request.

`build_hot_plates(request)` is the pure response-to-row conversion. The capture and
job adapters only fetch, attach event/timing metadata, and commit the result. Every
capture creates a new immutable version. The row key is
`(plate_code, date, captured_at)` and `available_at` equals `captured_at`.
Today-mode requests pass an empty API date and retain Kaipan's `Time`, `Min`, `Max`,
and `Day` metadata. History requests pass the explicit date.

## Positional fields

The source row has 19 positional fields. Confirmed positions are mapped to named
columns; unknown positions remain unchanged in `raw_fields`. Field status and
meaning follow `design/16-FIELD-CATALOG.md` and the audited Kaipan field facts.

| Position | Stored column | Meaning | Status / unit |
|---:|---|---|---|
| 0 | `plate_code` | Board/index code | CONFIRMED; Kaipan code |
| 1 | `name` | Board name | CONFIRMED; text |
| 2 | `strength` | Kaipan board strength | CONFIRMED field; integer, may be negative; scale and normalization UNKNOWN |
| 3 | `change_pct` | Board member average change | CONFIRMED; percent. Exact Kaipan member filters UNKNOWN |
| 4 | `raw_fields[4]` | No confirmed interpretation | UNKNOWN; raw |
| 5 | `amount_yuan` | Sum of member turnover | CONFIRMED; CNY |
| 6 | `raw_fields[6]` | Net amount, equal to [7]+[8] in audited sample | CONFIRMED; CNY |
| 7 | `raw_fields[7]` | Main/large-order inflow | CONFIRMED; CNY, positive direction |
| 8 | `raw_fields[8]` | Outflow | CONFIRMED; CNY, negative direction |
| 9 | `raw_fields[9]` | No confirmed interpretation | UNKNOWN; raw |
| 10 | `raw_fields[10]` | Approximate member float market-value sum | CONFIRMED approximation; CNY |
| 11 | `raw_fields[11]` | No confirmed interpretation | UNKNOWN; raw |
| 12 | `raw_fields[12]` | No confirmed interpretation | UNKNOWN; raw |
| 13 | `raw_fields[13]` | Approximate member total market-value sum | CONFIRMED approximation; CNY |
| 14 | `raw_fields[14]` | No confirmed interpretation | UNKNOWN; raw |
| 15 | `raw_fields[15]` | Constant-like value in audited sample; possible valuation ratio unverified | UNKNOWN; raw |
| 16 | `raw_fields[16]` | Constant-like value in audited sample; possible valuation ratio unverified | UNKNOWN; raw |
| 17 | `raw_fields[17]` | Observed duplicate of [2] | Raw duplicate; no separate interpretation |
| 18 | `raw_fields[18]` | Observed duplicate of [3] | Raw duplicate; no separate interpretation |

Strength is displayed only as Kaipan's raw strength. It is not a strategy score.
The 2026-10-27 sample had 19 snapshots and 2026-10-28 had 18; board 801159 was
present in 19 and 15 of those snapshots respectively. Its strength changed in both
directions during each day. Across the 37 saved snapshots (1,850 rows), observed
strength ranged from -228 to 46,792, with 171 negative values. This sample does not
establish a cross-day or cross-time scale, bound, or normalization; those remain
UNKNOWN.

## Snapshot schedule and failure handling

Each of the 18 scheduled events requests up to five 50-row pages. The retry timeline
below is derived from the event deadlines and profiles in `guardian/schedule.json`;
worst-finish assumes each try consumes its full timeout and each configured backoff.

| Event | Window (Asia/Shanghai) | Retry profile | Try starts (seconds after window start) | Worst finish / window |
|---|---|---|---|---:|
| `kaipan_hot_091610` | 09:16:10–09:17:30 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 80s |
| `kaipan_hot_092110` | 09:21:10–09:23:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 110s |
| `kaipan_hot_092520` | 09:25:20–09:25:50 | 2×10s, 2s×1 | 0, 12 | 22 / 30s |
| `kaipan_hot_092830` | 09:28:30–09:30:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 90s |
| `kaipan_hot_093220` | 09:32:20–09:33:30 | 3×12s, 4s×2 | 0, 16, 32 | 44 / 70s |
| `kaipan_hot_093520` | 09:35:20–09:36:30 | 3×12s, 4s×2 | 0, 16, 32 | 44 / 70s |
| `kaipan_hot_094020` | 09:40:20–09:41:30 | 3×12s, 4s×2 | 0, 16, 32 | 44 / 70s |
| `kaipan_hot_094520` | 09:45:20–09:46:30 | 3×12s, 4s×2 | 0, 16, 32 | 44 / 70s |
| `kaipan_hot_101000` | 10:10:00–10:12:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_102000` | 10:20:00–10:22:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_103000` | 10:30:00–10:32:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_104500` | 10:45:00–10:47:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_110000` | 11:00:00–11:02:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_112500` | 11:25:00–11:27:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_133000` | 13:30:00–13:32:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_143000` | 14:30:00–14:32:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_145000` | 14:50:00–14:52:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |
| `kaipan_hot_151000` | 15:10:00–15:12:00 | 4×12s, 5s×3 | 0, 17, 34, 51 | 63 / 120s |

The 09:25:20 auction event has a separate two-attempt profile because its window is
30 seconds. The 09:16:10 window is 80 seconds, and the 09:32:20–09:45:20 windows
are 70 seconds; remaining points have 90–120 seconds. A late start after a deadline does not call Kaipan: it records
a GAP. A failed event records a GAP with `late_policy=gap_only`, and links the latest
earlier snapshot as STALE when present; otherwise the fallback is MISSING. There is
no substitute source. The 15:15 reconcile event scans expired windows and records
missing GAP rows without a provider request.

## Coverage and storage contract

Coverage denominator is Kaipan `Total` (or `Count`); expected pages are
`ceil(Total / page_size)` and coverage is rows received divided by `Total`. A page-
limited response is stored with its actual rows and marked PARTIAL; missing rows are
never synthesized. Every source page must report the requested `Day`.

| Destination | Mapping / retention | Status |
|---|---|---|
| Redis intraday hot view | `core:cache:data:hot_plates:{trade_date}:{version}`, date pointer `core:cache:data:hot_plates:{trade_date}`, latest pointer `core:cache:data:hot_plates:latest`; 604800s TTL. Coordination key `core:cache:data:status`; 604800s TTL. | Key plan is in the generic publisher; C583 ran no Redis write/readback because Redis is read-only for this card. |
| TD close/history target | Logical candidate `core_data.hot_plates_v1`; columns `plate_code`, `date`, `captured_at`, `available_at`, `name`, `strength`, `change_pct`, `amount_yuan`, `raw_fields`, `source_day`, `event_id`; manifest carries coverage and Kaipan metadata. Keep all versions keyed by `(plate_code,date,captured_at)`. | No physical table verified or created; TD is read-only for this card. |
| Core file catalog | `hot_plates/trade_date=.../source=kaipan_getHisPlates/sha256-...`; immutable version ledger used for capture and contract checks. | Used for permitted real-source capture and readback; transition storage, not the intended long-term TD authority. |

The independent check used the 2026-10-28 15:05:06 hot-plate snapshot and the saved
`plate_members` plus Baostock daily rows. The all-member daily mean did not match
Kaipan [3] within 0.0005 percentage points for any of the first ten plates. This
comparison is reported as observed; membership filters are not known, so it does
not establish that Kaipan's confirmed filtered-member formula is wrong.

| Plate | Kaipan [3] % | Members | Daily rows | Daily mean % | Difference pp |
|---|---:|---:|---:|---:|---:|
| 801159 机器人概念 | 3.227000000 | 1305 | 1251 | 3.212971463 | +0.014028537 |
| 801573 三季报增长 | 2.425000000 | 1242 | 1193 | 2.412411903 | +0.012588097 |
| 801001 芯片 | 3.106000000 | 1135 | 1099 | 3.144931574 | -0.038931574 |
| 801199 汽车零部件 | 3.039000000 | 999 | 946 | 3.031419873 | +0.007580127 |
| 801660 通信 | 3.167000000 | 763 | 740 | 3.169566892 | -0.002566892 |
| 803023 AI应用 | 1.721000000 | 721 | 707 | 1.773208487 | -0.052208487 |
| 801045 医药 | 1.674000000 | 696 | 675 | 1.695021630 | -0.021021630 |
| 801843 商业航天 | 2.681000000 | 538 | 525 | 2.713998286 | -0.032998286 |
| 801366 业绩增长 | 2.236000000 | 2215 | 2178 | 2.232982874 | +0.003017126 |
| 801878 端侧AI | 3.083000000 | 355 | 344 | 3.116701453 | -0.033701453 |

Evidence outputs for the real strength sample, reconciliation, and retry timeline
are retained under `/home/exedev/fw/evidence/C583/`.
