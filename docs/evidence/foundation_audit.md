# Foundation audit: connectivity and wheel-local parity

Captured: 2026-09-04 (Asia/Shanghai)

This is a read-only audit of the existing production chain. It is evidence for
the independent `engine_core` project; it is not a runtime import boundary.

## Legacy Connectivity Inventory

| provider | legacy_location | client/library | host/config source | database/table/key | auth source | request/query | timeout/retry | session/pool | date/symbol format | response/error behavior | side_effect_risk | reuse_mode | verified_status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Redis Q2 | `engine_next/runtime/intraday_data_hub.py:115-165,1008-1021`; `C/t1_v2/config_v2.{h,cpp}` | `redis.Redis`, pipeline when available | server-local Redis; `REDIS_*` env/config; runtime default is localhost:6379/db0 | `q2:active:{YYYYMMDD}` set and `q2:{symbol}` hash; prefix is configurable | runtime environment/config; no secret copied | `SMEMBERS` then pipelined `HGETALL` | legacy hub catches read errors; no new retry policy | optional Redis pipeline | six-digit symbol; date `YYYYMMDD` | hash values are strings/bytes; missing key is empty, not zero | SAFE_READ | EXTRACT/WRAP only when Q2 wheel needs it | OBSERVED + live probe |
| TDengine historical tick | `C/t1_v2/td_replay_query.cpp`; `C/t1_v2/config_v2.{h,cpp}` | TD native client in server runtime | `TDENGINE_HOST`, default legacy table `stock_tick_v2`; server uses local TD service | `market_data1.stock_tick_v2` | server environment/config; no secret copied | event-time half-open slice, `ORDER BY ts ASC, symbol ASC`; milli price and integer yuan/unit columns | legacy query builder; no new retry policy | client-managed by runtime | epoch-ms boundaries; six-digit symbol | query rows; replay does not contain Rabbit arrival/batch facts | SAFE_READ | WRAP | VERIFIED query shape + live probe |
| TDengine previous-day daily data | `engine_next/offline/integrated_sync.py`; `web/services/tdengine_service.py` | `taos`/`TDengineService` | server-local TD, database `market_data1`; service default 127.0.0.1:6030 | `daily_kline` stable, tag `symbol` | server environment/config; no secret copied | date-bounded read through existing TD service | existing service handles connect failures; no new retry policy | thread-local connection in legacy service | date `YYYY-MM-DD`, symbol as stored tag | FLOAT/BIGINT rows; observed sample has zero volume and requires audit | SAFE_READ | WRAP | VERIFIED schema + live probe; volume semantic UNKNOWN |
| Q2Frame | `C/t1_v2` replay/runtime files | existing C++ replay path | configured `REPLAY_Q2FRAME_PATH` | file path, not yet used by engine_core | local runtime config | existing file/replay path | existing runtime behavior | file reader | configured replay dates | not probed in this slice | SAFE_READ | KEEP until Q2Frame wheel is in scope | UNKNOWN |
| RabbitMQ tick source | `C/t1_v2/rabbitmq_tick_source.cpp`, `rabbitmq_wire_message.*` | existing C++ consumer | `RABBITMQ_*` environment/config | queue defaults to `stream2` | server environment/config | existing consumer/ACK path | existing C++ behavior | channel-owned ACK | source message format | not consumed by this audit; do not touch production queue | MAY_MUTATE | KEEP | UNKNOWN for engine_core |
| Network historical sources | `engine_next/connectors/wencai_connector.py`, `kaipan_connector.py`, `baostock_connector.py` | existing connector/session implementations | runtime configuration and connector defaults | source-specific query APIs | cookie/session/config; secrets excluded | existing connector methods | connector-specific | connector/session-specific | source-specific | not probed because current wheel does not require them | MAY_MUTATE | register only; no extraction | UNKNOWN |

## Connectivity Parity

| source | legacy_access_behavior | new_module | reuse_mode | reason/evidence |
| --- | --- | --- | --- | --- |
| Redis Q2 | active set + per-symbol hash; optional pipeline; configurable prefix | future thin Q2 provider/adapter | EXTRACT/WRAP | Q2 wheel requires the existing key/query semantics; no second Redis hierarchy |
| TD `stock_tick_v2` | safe table allow-list, `[start,end)` query, timestamp/symbol ordering | future replay adapter | WRAP | replay is deferred; preserve query shape and do not claim arrival order |
| TD `daily_kline` | existing `TDengineService` connection/query path | first previous-day provider when needed | WRAP | reuse service behavior; do not create a new TD access framework |
| RabbitMQ | C++ consumer owns connection/channel/ACK | outside engine_core | KEEP | engine_core must not consume or ACK the production queue |
| Wencai/Kaipan/BaoStock | existing connectors with sessions, query parameters and source-specific policies | not in current wheel | KEEP | only record existence until a real acceptance case needs one |

## Wheel-local Legacy Parity

| legacy_capability | legacy_location | legacy_field_or_function | new_wheel | behavior_contract | parity_status | evidence | fixture |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q2 active universe uses date-tagged Redis set | `engine_next/runtime/intraday_data_hub.py:115,1017`; `C/t1_v2/redis_v2_writer.cpp:46-52` | `q2:active:{YYYYMMDD}` | `normalize_q2` / Q2 adapter | read the declared date cohort; missing hash is missing, never zero | MATCH (observed) | live probe manifest | pending captured Q2 fixture |
| Q2 prices are milli-integers | `engine_next/runtime/intraday_data_hub.py:198-204`; `C/t1_v2/redis_v2_writer.cpp:54-91` | `px`, `pc` | Q2 Field Contract | preserve integer milli value; convert only at an explicit boundary | MATCH (verified) | producer source + live sample | pending |
| Q2 writes source timestamp and phase fields | `C/t1_v2/redis_v2_writer.cpp:54-91` | `ts`, `ph`, `ls` | Q2 Field Contract | invalid/missing core timestamp is not READY | INTENTIONAL_CHANGE | producer source; old reader defaulted absent values | pending |
| equity filtering excludes indices/ETFs from stock analysis | `engine_next/runtime/intraday_data_hub.py:72-83,1019` | `_is_q2_equity_quote` | `classify_equity` | classify explicitly; do not let non-equity rows contaminate stock facts | MATCH (observed) | legacy source | pending |
| snapshot triggers have vendor settling delays | `C/t1_v2/snapshot_trigger.cpp:13-27` | A20 `09:20:03`, A24 `09:24:10`, A25 `09:25:06` | Window/trigger fixtures | distinguish requested wall time from observed fired time | MATCH (verified) | C++ source | pending |
| TD replay is timestamp/symbol ordered, not Rabbit arrival replay | `C/t1_v2/td_replay_query.cpp:24-57` | `ORDER BY ts ASC, symbol ASC` | future replay adapter | do not label deterministic event-time ordering as production arrival order | MATCH (verified) | C++ source | pending |
| daily kline volume is a legacy field with observed zero values | `web/services/tdengine_service.py`; live probe | `daily_kline.volume` | `PreviousDayStatsFunction` | do not infer missing/zero semantics until source behavior is verified | UNKNOWN | live probe shows zero in sampled rows | pending |
| Q2 cumulative amount and volume units | `C/t1_v2/raw_tick_converter.cpp:85-86`; `C/t1_v2/quote_state.h:25-27`; `C/t1_v2/quote_calculator.cpp:65-77` | `amt`, `vol`, `amt2m`, `amt5m` | Q2 Field Contract / SegmentFrame | amount is integer yuan and cumulative; volume is shares and cumulative; deltas require explicit cumulative semantics | MATCH (verified) | producer source and C++ self-test | captured TD/Q2 fixtures |
| Q2 auction amount proxy units | `C/t1_v2/auction_calculator.cpp:11,80-105`; `C/t1_v2/redis_v2_writer.cpp:83-88` | `am`, `br`, `ar` | Q2 Field Contract / resting pressure | values are integer yuan; `br/ar` use level-2 price and shares; proxy is not authoritative net inflow | MATCH (verified) | producer source and C++ self-test | Q2 fixture |

## Probe conclusions

- Redis and TD are reachable on `cobra-ion` through the existing server runtime environment.
- Redis `q2:active:20260904` was readable with full coverage in the later read-only check; the older 20260903 cohort was found to contain cross-day source timestamps and is not an immutable historical snapshot.
- Q2 hash contains `px, pc, amt, vol, iv, ia, ln, ts, ph, ls, mx, mn, spd1m, amt2m, amt5m, vec3m, vec5m, a20, a24, a25, am, br, ar, mk` in the sampled cohort. Only fields needed by the current wheel are candidates for the canonical model.
- TD `stock_tick_v2` has the expected integer/milli schema and returned 82,183 rows for 2026-09-03 09:20-09:24 in the read-only probe.
- TD `daily_kline` is readable, but sampled `volume=0` values are not yet a verified semantic zero; this remains UNKNOWN.
