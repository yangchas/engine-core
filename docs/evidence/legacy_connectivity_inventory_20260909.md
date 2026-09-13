# Legacy connectivity inventory — 2026-09-09

This is a behavior inventory for the production `engine_next` release
`e272842c8f490f55a1b017badb71e71904ce008e`. It is not a new access framework and it does
not grant permission to consume Rabbit or write production storage. The inventory records how
the existing system obtains data so a future thin provider can reuse the verified path.

## Rules

- Compare capabilities, not legacy functions or file layout.
- Reuse an existing client, authentication, query parameters, symbol/date format, timeout and
  session behavior before considering a rewrite.
- `SAFE_READ` means the inspected path only reads the source. `MAY_MUTATE` means the call can
  write cache/storage or trigger recovery. `UNKNOWN` requires a side-effect audit before use.
- Connectivity success is not historical `available_at` evidence.
- A provider may return raw/normalized source data; business meaning belongs to a DataFunction.

## Inventory

| Source / dataset | Legacy access path | Parameters and observed semantics | Side-effect risk | New-core status |
|---|---|---|---|---|
| Redis Q2 | `engine_next.runtime.intraday_data_hub.IntradayDataHub._batch_hgetall`; `q2:active:{YYYYMMDD}` then `q2:{symbol}` | Pipeline HGETALL when available; `px/pc/mx/mn` milli-price; `amt/ia/ln/amt2m/amt5m/am/br/ar` integer-yuan fields; `vol` is board lots; `ts` is preserved source-record time; active set is a rolling cohort, not an atomic historical snapshot | `SAFE_READ` for adapter path; Q2 writer is outside core | `RedisQ2ProjectionAdapter` reads the same key shape; real read PASS, freshness currently STALE |
| Redis auction projections | `_read_auction_hash_rows`, `_read_auction_summary`, `_decode_archived_anchor_rows` | `market:auction:{date}:{0920,0924,0925}` contains summary/TopN projections; `market:auction:anchor:{date}` is a candidate archive, not full P/M/RB/RA universe | `SAFE_READ` for readers | no canonical adapter yet; only shared fields may be compared |
| Auction recovery | `IntradayDataHub.recover_auction_anchor` | Redis anchor → Redis 0925/legacy preview → TD → Wencai; successful recovery can write Redis anchor | `MAY_MUTATE` | intentionally not imported into core read path |
| TD daily kline | `RuntimeKlineService.tdengine.get_daily_kline`; production taos path used by `TDPreviousDayStatsProvider` | Explicit symbol/date range; stored `daily_kline` has no publication/insertion timestamp; current rows normalize to close/amount plus observed `volume=0` | `SAFE_READ` in provider query; persistence method is separate and mutating | thin provider boundary exists; runtime result remains UNAVAILABLE when `available_at` is unknown |
| BaoStock daily kline | `BaostockConnector.fetch_daily_kline` → `fetch_daily_kline_range` | Reuses process session/login lock and query lock; `query_history_k_data_plus`; fields date/code/OHLC/preclose/pctChg/amount; `frequency='d'`, `adjustflag='3'`; one session reset retry on error `10001001` | `SAFE_READ` (external read; no Redis/TD writer in connector) | online connection/date contract PASS; DataFunction wrapper still minimal |
| BaoStock calendar | existing BaoStock calendar capture used to build immutable snapshot | Explicit source date set; guard dates extend outside declared runtime coverage | `SAFE_READ` during capture | core calendar snapshot is runtime authority |
| Kaipan hot plates | `KaipanConnector.fetch_hot_plates(trade_date, today_mode)` → `StockAnalyzer.get_his_plates` or `_call_api('getHisPlates','')` | Historical and current modes are distinct; response rows are source-ranked and not always self-dated | `SAFE_READ` in connector; cache writeback belongs to runtime hub | online connection PASS/OBSERVED; no historical runtime promotion without date evidence |
| Kaipan yesterday limit pool | `fetch_yesterday_bans_pool(trade_date, max_ban)` | Explicit query date and bounded `max_ban`; response date is not required by current parser | `SAFE_READ` in connector | online connection PASS/OBSERVED; DataFunction not migrated |
| Kaipan ban reasons | `fetch_ban_reasons(symbol)` | No structured requested date; `source_trade_date` must match target before historical use | `SAFE_READ` in connector | online connection PASS/OBSERVED; historical runtime remains guarded |
| Wencai limit truth | async `fetch_limitup_with_lb_days(max_stocks)` plus normalize | Current query has no structured date parameter; result is useful as current/oracle evidence only unless dated query evidence is captured | `SAFE_READ` | online connection PASS/OBSERVED; historical replay role unavailable |
| THS hot rank | async `fetch_hot_rank(top_n)` → `get_trending_stocks(... include_tags/include_topic)` | No date parameter; low-frequency attention proxy; should be cached, not polled at tick frequency | `SAFE_READ` | online connection PASS/OBSERVED; historical replay unavailable |
| Rabbit raw stream | t1-v2 `RabbitMqTickSource`; `t1-v2-live.service` owns consumer and ACK | Queue `engine_next_external`; one delivery per runtime loop; direct core consumer is forbidden in this phase | `MAY_MUTATE` (ACK/requeue) | `KEEP` outside core; use runtime counters/passive declare only |

## Important legacy behavior differences

1. `IntradayDataHub._standardize_q2_quote` defaults missing numeric fields to zero and derives
   phase-specific auction fields. Core Q2 normalization preserves `Missing` and does not let a
   default zero become a fact. This is an intentional semantic correction, not a compatibility
   alias.
2. `RuntimeKlineService.get_start_date_by_trading_days` delegates to the legacy calendar and
   silently stops after a two-year guard. Core derives the previous trade date from a versioned
   exchange-date snapshot and fails closed outside coverage.
3. `recover_auction_anchor` is not a read-only loader even though its name starts with “recover”.
   It must not be called from a shadow/readiness probe.
4. Current Redis Q2 coverage of 1.0 does not imply freshness or a same-time market snapshot.
5. A successful network request today does not establish that the result was available at a
   historical replay cutoff.
6. The legacy `web.services.tdengine_service.TDengineService` constructor is not a safe Core
   boundary: its initialization path can execute `CREATE DATABASE/STABLE` checks.  Core probes
   therefore must not instantiate that service.  The current thin TD provider receives an
   injected read-only callable using the same production table/date query semantics; this is an
   intentional access-boundary extraction, not a new TD storage layer or a permission to write.

## Next extraction boundary

The first permitted extraction is a thin `TDPreviousDayStatsProvider` wrapping the existing
read-only taos query behavior, followed by `PreviousDayStatsFunction` and `TemporalDataGuard`.
The next permitted live reader is the existing Redis Q2 key path. No generic Redis/TD/network
access hierarchy, Rabbit consumer, writer, recovery framework or fallback engine is introduced.

## Evidence status

- Real online probe on cobra-ion: all six external connector calls connected; only BaoStock had
  explicit request/response date closure.
- Real Redis Q2 on 2026-09-09: 5,218/5,218 rows, all stale under 300 seconds at the late-day
  observation.
- Real Rabbit passive declare: backlog later reached zero; t1 observability release
  `9fd4a42b3f3944235da89e1ae2278ea93cff193c` is now running as
  `/home/exedev/services/t1-v2/releases/20260909_9fd4a42` for the next trading-day counters.
