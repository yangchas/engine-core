# M0 current-release startup flow audit (2026-09-14)

## Scope and safety

This is a read-only audit of the production `engine-next` release and the
existing `t1-v2-live` runtime on `cobra-ion`.  It does not add a consumer,
change Rabbit acknowledgements, write Redis or TDengine, invoke repair/fetch
paths, send mail, or stop/restart either production service.

Observed at approximately `2026-09-14 11:10-11:13 Asia/Shanghai`.

## Runtime identity

| Item | Observed value |
|---|---|
| Host | `cobra-ion` |
| `engine-next` path | `/home/exedev/services/engine-next/releases/20260903_e272842` |
| `engine-next` service | `active`, `MainPID=4022407`, `NRestarts=0`, started `2026-09-14 00:30:01 CST` |
| `t1-v2-live` service | `active`, `MainPID=2878024`, `NRestarts=0`, started `2026-09-09 22:31:00 CST` |
| engine runtime | Python `3.12.3`, systemd `TZ=Asia/Shanghai`, `PYTHONPATH=.` |
| engine entry | `python -m engine_next.app_main --environment server` |
| t1 entry | `/home/exedev/services/t1-v2/current/content/bin/t1_v2` |
| release Git metadata | not present in the deployed release directory; identity is path plus source-file hashes |

Source hashes recorded from the deployed release:

```text
engine_next/app_main.py                                      bd8aaed4753f991f94e46e71e8b0a0012a4f9f3c48b3b743c91e57f069199803
engine_next/runtime/startup_runtime_coordinator.py           079c3a38d1b5282c37bb8de6bbfe3468b16c8b7385fc228113b5b5dc0d0fe8f0
engine_next/runtime/startup_self_check.py                    6f844818245ad0b8c7afd2fe1ba860e21a55f7da53e887938eb813c74836cc7c
engine_next/runtime/intraday_data_hub.py                     645ab676d921c6437b28efce3535b4ee32cac8483334610c5dc30e532f634c5b
engine_next/runtime/intraday_context_builder.py              255412981882ee9f916f273c011e8370c1014ab39a6aa7bc1252613944b55654
engine_next/runtime/original_timeline.py                     72e082d8f08ec2c0e9fc417d4c9ccf40d96d52974f08bd18b727a176763b6da6
```

The `original_timeline.py` hash line above was captured from the release and
is retained as source evidence; the active service has no repository commit
pointer to use as a replacement identity.

## Startup and event timeline

The current release implements the following behavior.  The `engine_v2`
timeline remains the migration baseline; this table records what is present in
the current release, not an assertion that every path has run successfully in
the observed process.

| Window/node | Current owner and trigger | Read inputs | Possible side effects | M0 status |
|---|---|---|---|---|
| `00:00-09:15` | `EngineApp` request construction and startup bootstrap | calendar, previous date, Redis readiness maps, watermarks | bootstrap may run offline sync and repair actions | `OBSERVED` in source |
| `01:00-08:30` | legacy `DataLifecycle.on_startup` recap branch | calendar, previous trade day | spawns recap process and writes lifecycle state | `OBSERVED` in source; no production replay invoked |
| `08:30` | `STARTUP_AUDIT_CHECKPOINTS` | runtime readiness and watermarks | startup coordinator may load mapping, refresh caches, rebuild summary | `OBSERVED` in source |
| `09:00` | second startup audit checkpoint | same as 08:30 | same possible repair paths | `OBSERVED` in source |
| `09:15-09:20` | `t1.cpp` / live runtime | Rabbit-delivered ticks, Q2 state | Redis/TD writer path owned by t1-v2 | `OBSERVED` from t1 counters; batch membership `UNKNOWN` |
| `09:20` | t1 snapshot emission | auction state | Redis `market:auction:{date}:0920` and related projection | key exists in current Redis; upstream writer ordering `UNKNOWN` |
| `09:24` | t1 snapshot emission | auction state | Redis `market:auction:{date}:0924` and related projection | current key exists, historical capture slot was missing; no backfill of capture |
| `09:25` | `EngineApp._build_loop_decision`, earliest finalize `09:25:10` | runtime quote/auction readiness | controller may recover/finalize and write caches | trigger rule `OBSERVED`; final tick batch `UNKNOWN` |
| `09:26` | scheduled follow-up event | auction/previous-day data | refresh yesterday pool and auction analysis/reporting | `OBSERVED` in source; live loader trace `UNKNOWN` |
| `09:30+` | intraday loop | Redis Q2 and runtime caches | intraday analysis, possible controlled network repairs | current Q2 source is stale; no Core production action |
| `09:32:10-09:34` | opening facts checkpoint | auction facts plus Q2 cutoff | report/projection path | `OBSERVED` in source; engine-next loader trace `UNKNOWN` |
| `15:05` | market close marker | wall clock | marks close and slows loop | `OBSERVED` in source |
| `17:40+` | settlement/offline lifecycle | TD/Baostock/Kaipan/Wencai according to phase | offline sync, factor/recap persistence, notification owner | outside this read-only capture |

`EngineApp.run_forever()` polls at a default 30-second interval and performs a
post-run due check.  This is a scheduler behavior, not a replacement for the
source batch/freeze contract.

## Getter/action inventory

| Dataset/action | Current source and key/table | Read/write classification | Why Core cannot call the current method directly |
|---|---|---|---|
| Calendar/previous date | `TradingCalendarService` in `build_default_request` and `_default_previous_trade_date` | read, with a broad timedelta fallback on exception | fallback can hide calendar authority failure; Core uses versioned `TradingCalendarSnapshot` instead |
| Q2 | Redis `q2:active:{date}` plus `q2:{symbol}`; `IntradayDataHub.fetch_online_q2_rows` | read-only at the source boundary | suitable only after Core Q2 adapter applies source-time, coverage and freshness rules |
| Auction snapshots | Redis `market:auction:{date}:0920/0924/0925`, fields `meta/summary/top_amount`; `load_auction_snapshots` | read-only | projection is TopN/summary, not raw batch or full AuctionState |
| Auction anchor recovery | `IntradayDataHub.recover_auction_anchor` | **may write** `market:auction:anchor:{date}` and may call TD/Wencai fallback | prohibited in Core shadow/read path |
| Hot plates | `fetch_hot_plates` and Kaipan connector | external read plus Redis write/meta update | current metadata lacks verified schema, units and availability; Core keeps result `UNAVAILABLE` |
| Yesterday limit pool | `fetch_yest_limit_pool` and Kaipan connector | external read plus Redis write/meta update | current metadata lacks verified availability/units; Core keeps result `UNAVAILABLE` |
| Stock/plate mapping | `StartupStaticDataLoader.load_stock_plate_mapping` | CSV read plus Redis HSET/pipeline write | must remain an external owner; Core consumes frozen mapping only |
| Runtime market summary | `MarketRuntimeSummaryService.load_cached` is read-only; `get_or_build/build_and_write` writes two Redis keys | mixed | only cached read is eligible for a read-only Core path |
| TD auction | `build_readonly_td_auction_query` used by production reporting | read-only query | TD rows are an oracle/projection source, not proof of Rabbit batch membership |
| F10 names | `F10DataService.batch_get_stock_names` | external read | auxiliary identity/display data; must be bounded and outside the reducer |

## Real read-only evidence

The following commands were executed through the persistent Cobra SSH broker
using the production shared Python/Redis client.  They used only `TYPE`,
`HLEN`, bounded `HSCAN`, `SMEMBERS`, `HGETALL` and file/log reads.

```text
q2:active:20260914                         set, SCARD=5220
market:auction:20260914:0920               hash, HLEN=3 (meta/summary/top_amount)
market:auction:20260914:0924               hash, HLEN=3 (meta/summary/top_amount)
market:auction:20260914:0925               hash, HLEN=3 (meta/summary/top_amount)
market:auction:anchor:20260914             string, current payload present
```

The current 0924 key is a later observation.  The archived trading-day
capture still records `auction_0924` as a failed required slot and remains
`PARTIAL`; that historical evidence is not rewritten with the later key.

The exact Core Q2 probe at `2026-09-14 11:12` reported:

```text
requested/received       5220/5220
coverage                 1.0
status                   STALE
consistency              BEST_EFFORT_STALE
stale symbols            5220
oldest source time       2026-09-14 00:00:00+08:00
newest source time       2026-09-14 10:42:57+08:00
newest source lag        about 1850 seconds
projection hash          51f216bb626e095be093f290cd11d4a9c55a018a248cc3e94c92fb5725b24bb7
input hash               12b337838af1eac0520750982bc26d88193d8c005edfd34544118eb62ba3bfb2
artifact sha256           7cd839d09445a6edb6d0c5a75601effa946568d7302fc8d0e6c5ad1e451c2fd5
read operations           SMEMBERS=1, HGETALL=5220
side-effect proof         only Redis reads; no TD/claim/notification/SMTP assembly
```

The t1 runtime log continued to show `ack_fail=0` and active progress, but the
latest source timestamp lagged wall time by roughly 30 minutes.  This is a
real data freshness result and was not “repaired” by Core.

## First-divergence and blocker table

`FIRST_DIVERGENCE` is only assigned when the previous layer has already been
proved semantically equal.  Otherwise the result is `UNPROVEN`.

| Boundary | Status | Evidence/interpretation | Current action |
|---|---|---|---|
| Gateway/Rabbit receive/decode/batch membership | `UNKNOWN` | t1 counters prove ongoing processing, but no batch id/emit flag/source range in logs | do not add a consumer; consider a future default-off audit hook only if this blocks a decision |
| Runtime → AuctionState finalization | `UNKNOWN` | source code has 09:25:10 earliest finalize, but no direct state/update trace | do not infer from TD row order |
| AuctionState → Redis/TD writer equality | `UNPROVEN` | Redis and TD projections can be read, but common upstream state is not observed | compare only authority-matrix fields when both sides have matching source/anchor |
| Redis Q2 source → Core Q2 adapter | `OBSERVED` | 5220 rows, deterministic repeat hash; all stale under 60s policy | Core shadow allowed, result remains `STALE` |
| Reference data → Core DataResult | `UNAVAILABLE` | hot plates and previous-day limit pool lack verified `available_at_ms/schema/field_units` | fail closed; no historical replay use |
| engine-next loader → report | `UNKNOWN` | current release has loader/report code, but no side-effect-free trace was captured | do not claim engine-next consumption parity |
| Core → shadow trace | `OBSERVED` | real Q2 read-only probe is deterministic; auction TD reconstruction is fact-only | no strategy/effect/production replacement |

## M0 result

```text
CURRENT_RELEASE_IDENTITY          OBSERVED
STARTUP_TIMELINE                  OBSERVED (source-level)
READ_ONLY_Q2                      OBSERVED / STALE
AUCTION_PROJECTION_KEYS           OBSERVED
REFERENCE_DATA_READY              UNAVAILABLE
RABBIT_BATCH_MEMBERSHIP           UNKNOWN
AUCTIONSTATE_FREEZE_ORDER         UNKNOWN
ENGINE_NEXT_LOADER_TRACE          UNKNOWN
CORE_STARTUP_COORDINATOR          NOT_IMPLEMENTED
```

M0 is therefore an audit result, not a replacement gate.  The next bounded
implementation remains a read-only Core startup/readiness composition using
the already verified Calendar/Q2/Window/Data contracts.  It must not call the
mutating legacy Hub/controller methods listed above.  No strategy migration is
authorized until a legacy rule has a verified consumer oracle and a real
fixture.
