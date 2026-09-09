# 2026-09-09 real integration and test-truth audit

## Scope and safety

All server actions were read-only or isolated validation actions. No Rabbit consumer was added, no ACK behavior changed, no Redis/TD row was written, and no production service was restarted. `engine_core` ran from `/home/exedev/validation/engine-core-3fc07bd`; production services remained on their existing releases.

## Exact identities

- engine_core commit: `3fc07bd77d2a361bbd269978b4ee45040497b308`
- uploaded archive SHA-256: `fcc10a0dca60f315d3c00bfb774283c6f88dbc2d5e164791ce8aa0ea5879c684`
- production engine_next commit: `e272842c8f490f55a1b017badb71e71904ce008e`
- production t1-v2 commit: `6fb3164baab00d840886da5f056587ec32f3d86a`
- Linux runtime: Python 3.12.3

Post-audit verification addendum:

- final audited engine_core commit: `db2cd56b20a37ec120c6a5ee3f639275f082af54`
- final tracked archive SHA-256: `aee473ba8aa7f95d0368576c7621ef874524e25b1a0cc97ea6052a48c4dd0104`
- isolated Linux path: `/home/exedev/validation/engine-core-db2cd56`
- identical local/Linux default suite: 156 passed
- the two added tests cover production clock adapters and real BaoStock calendar-fixture answers; they do not add online-provider claims

Final audit addendum:

- audited wrapper commit before the trace test: `fa73f65850f0da97a4dad3022c0938ea1f4ecce5`
- exact tracked archive SHA-256: `97e1c9d2f031618902a81d39da87d92d2e0c8942ae0a740bed4e11314e44320f`
- isolated Linux path: `/home/exedev/validation/engine-core-fa73f65`
- identical local/Linux default suite at that commit: 156 passed
- `417812a` then added the previously missing direct `JsonTraceSink` contract tests;
  final local/Linux identity is recorded after the concluding deployment

## What the engine_core tests really are

The default suite is an offline deterministic contract suite. It contains pure unit tests, state-machine tests, and tests driven by captured production fixtures. It does not open live Redis, TD, Rabbit, BaoStock, Kaipan, Wencai, or THS connections.

The files named `test_live_q2_probe.py` and `test_real_reference_probe.py` test the probe logic with fake clients. They do not constitute online integration evidence. Captured fixture tests such as the 600519 auction pair are real-data-derived but remain offline and immutable.

Local Windows and isolated Linux both passed the exact 154-test suite. This proves cross-platform deterministic behavior for the tested contracts, not end-to-end production readiness.

The evidence classes must not be conflated:

| Test/evidence class | Examples | Opens a real connection during pytest | What it proves |
|---|---|---:|---|
| pure contract/state test | hash, clocks, windows, timers, evaluation ownership | no | deterministic behavior of supplied values |
| synthetic fixture test | boundary, missing, conflict and adversarial cases | no | fail-closed/error semantics |
| captured production fixture test | BaoStock calendar, TD daily rows, 600519 auction anchors | no | repeatable behavior for an immutable real-data sample |
| fake probe test | `test_live_q2_probe.py`, `test_real_reference_probe.py` | no | probe logic only |
| cobra-ion online probe | Redis Q2, TD reads, BaoStock/Kaipan/Wencai/THS | yes, outside pytest | current connectivity/schema/observed data only |
| production runtime observation | systemd, logs, Rabbit passive declare | yes, read-only | actual running-chain state within the observable boundary |

No default pytest test currently proves live Rabbit delivery/ACK, online provider availability,
startup repair, persistence, email delivery, or a full production lifecycle.

## Real connections executed on cobra-ion

### Redis Q2

- source: loopback production Redis, `q2:active:20260909` plus 5,218 per-symbol `q2:{symbol}` hashes
- coverage: 5,218 / 5,218, no missing symbol
- repeated observations: all rows were stale under 120s and 300s explicit thresholds
- newest lag increased from about 193s to 471s during the audit
- result: `REAL_Q2_READ_PATH=PASS`, `LIVE_Q2_FRESHNESS=NOT_PASS`
- same immutable projection through two Engine instances produced equal hashes

Volume dimensional checks for `000001`, `300750`, and `600519` showed `amount / (raw_volume * 100)` within about 0.4% of the current price, while the shares hypothesis was about 100 times the current price. This is strong production evidence that cumulative `vol` is board lots.

### TDengine

- production read-only `auction_snapshot_v2` query for `2026-09-09/0925`: 5,217 rows
- fields include `px_milli`, `match_amt_yuan`, `rest_bid_amt_yuan`, and `rest_ask_amt_yuan`
- `daily_kline` schema has no publication or insertion timestamp; it cannot prove historical `available_at`
- result: connectivity and current stored facts pass; historical knowledge availability remains unavailable unless separate evidence supplies it

At 14:13 the latest TD tick was 14:01:15 and the latest Redis Q2 source time was 14:01:39. Their 24-second difference is much smaller than their common roughly 12-minute wall-clock lag. This rules out a Redis-only writer delay and places the first proven delay boundary before the Redis/TD fork.

### Rabbit/t1-v2 boundary

An AMQP passive queue declaration (no consume, no delivery, no ACK) reported one consumer and a backlog increasing from 2,457 to 2,469 messages. The t1-v2 process had established Rabbit, Redis, and TD TCP connections. Combined with the Redis/TD timestamp alignment, this is direct evidence that the live consumer is not keeping up with the producer at the audit time.

At 15:19 Asia/Shanghai the same passive declaration reported 2,602 messages and one consumer.
The backlog was lower than the 15:03 observation but remained non-zero after the close. The
actual production unit is `t1-v2-live.service`; both it and `engine-next.service` were active with
zero restarts. A query against the nonexistent shorthand unit `t1-v2.service` must not be used
as production status evidence.

The current defaults process one Rabbit message per loop with a 10ms configured delay. This observation does not yet prove whether decode, TD insertion, Redis command volume, or another per-message step is the dominant cost, because per-stage live counters are not exposed.

A bounded observability patch was prepared from the exact production t1-v2 base on branch
`codex/fix-t1-live-observability`, commit
`9fd4a42b3f3944235da89e1ae2278ea93cff193c`.  It reports cumulative
batch/source/ACK/Redis/TD counts plus last-batch pipeline, commit, ACK and wall-lag
durations.  The v4 archive SHA-256 is
`d6006539f4abc599665d79e7b38cde3b4b708beb2cbeaebebdd9c92cfcd98228` and the
cobra-ion full-dependency candidate binary SHA-256 is
`0698b4172b58248bb1eaf4b3efa6d18a8c1fac78334a250455fdff9a3ed84563`.
Its built-in self-test passed.  It was not installed or started as production,
so it closes build/test feasibility only, not the live bottleneck diagnosis.

### External providers

| Provider | Connection | Contract result | Historical date authority |
|---|---|---|---|
| BaoStock daily kline | PASS | PASS | explicit request and response date |
| Kaipan hot plates | PASS | OBSERVED | request date; response not self-dated |
| Kaipan yesterday pool | PASS | OBSERVED | request date; response date not required by current parser |
| Kaipan ban reasons | PASS | OBSERVED | current/result source date requires separate validation |
| Wencai limit truth | PASS | OBSERVED | current query; no structured historical date |
| THS hot rank | PASS | OBSERVED | current query; no date parameter |

The raw evidence files on cobra-ion are:

- `reference-sources-20260909-3fc07bd.json`, SHA-256 `b9c664b034ee2bf69a690032910817d2c20375261232bd0475247497de5a37f8`
- `live-q2-20260909-3fc07bd.json`, SHA-256 `1befa207ad5abd725292abbc7c596c175764cc5253c1dce987b163de493e1524`

A bounded second online run at `2026-09-09T07:24:50Z`, using the unchanged production
connector release, again passed all six connection calls. BaoStock alone closed the explicit
request/response date contract; Kaipan, Wencai and THS remained observation-only for historical
use. Evidence: `/home/exedev/validation/engine-core-fa73f65/reference-sources-20260909-1519-fa73f65.json`,
SHA-256 `c035f640ff772703d0b7f7ea44ce4cee00d959b8fb45644fee48e119ad9db977`.

A second production Redis observation at `2026-09-09T07:25:24Z` read all 5,218 active
symbols with no missing row, but every symbol was stale under the explicit 300-second policy.
The newest source record lag was 1,725 seconds and two Engine instances still produced the same
semantic result. Evidence: `/home/exedev/validation/engine-core-fa73f65/live-q2-20260909-1525-fa73f65.json`,
SHA-256 `337e0c309418db599abeb8342554ae84b1c63c61ec69b59d53dea87633154957`.

## Production runtime observations

- `engine-next.service` and `t1-v2-live.service` remained active with zero systemd restarts.
- t1-v2 had established TCP connections to the remote Rabbit broker, local Redis, and local TDengine.
- 09:25 finalize and 09:26 follow-up executed in the real production log.
- email delivery was logged at 09:26 and 09:32.
- engine_next later logged `live_quote_ready=False`, matching the independent Q2 stale probe.
- At 09:56 the production log emitted `intraday stale gate | readiness=observe_runtime | live_quote_ready=False` while downstream hypothesis/context code still calculated `market=attack_confirmed`, candidates and profit-center rows.  This is a legacy readiness-ownership defect, not behavior to preserve as parity: the new path must downgrade before facts/strategy and must not rely on the presentation controller to hide stale conclusions.
- t1-v2 production binary `--self-test` passed, but that path uses internal fake sources/executors and is not evidence of actual Rabbit delivery or ACK counts.

The production t1-v2 config declares a file log path, but current C++ runtime code never wires `logging.file_path` or `enable_file_log` to a file sink. The long-running process only exposes summaries on exit and transient errors. Consequently Rabbit batch membership, decode counts, ACK counts, and per-batch Redis/TD commit counts remain unobservable during normal operation.

Redis source types must remain separate. On 2026-09-09 the three `market:auction:*:{0920,0924,0925}` hashes contained only `meta/summary/top_amount` projections. The `market:auction:anchor:20260909` JSON contained 5,183 symbols but only `change_pct/amount/bid_amount/tag/source`; it is not a full P/M/RB/RA authority. For symbol `000001`, the corresponding TD 0925 row additionally contained `px_milli` and `rest_ask_amt_yuan`, proving that cross-source equality must be limited to shared fields.

## Legacy test suite integrity

Running all packaged production engine_next tests explicitly (`pytest engine_next/tests/*.py`) produced 289 passes and 32 failures. The release files match the declared Git commit after normalizing CRLF; this is not a post-deploy edit. Failures include stale constructor expectations, missing packaged fixtures, and mojibake assertions. Therefore the old release's checked-in tests are not a clean release gate.

## Migration coverage

| Capability | engine_core status | Evidence class |
|---|---|---|
| immutable contracts/hash/time/calendar | implemented | offline unit + captured fixture + Linux parity |
| Redis Q2 current projection | implemented | real online read |
| TD previous-day thin provider | implemented boundary | real read; historical availability unresolved |
| Q2Frame replay | implemented | offline/captured fixture |
| TD event-time replay | implemented | real TD rows; synthetic tie-break only |
| session/timer/evaluation ownership | implemented in-memory | deterministic unit tests |
| Redis auction/anchor canonical adapter | not implemented | old production only |
| startup self-check and on-demand repair orchestration | not migrated | old production behavior observed |
| persistent checkpoint/restart reconstruction | not implemented | deferred |
| BaoStock/Kaipan/Wencai/THS runtime DataFunctions | not implemented | connectivity probes only |
| theme/leader/large-cap/extreme/yesterday-limit/style facts | not migrated | old production only |
| opening validation and state lifecycle | not migrated | old production only |
| report/email/effect | not implemented | ProbeStrategy only; old production sends email |
| full replay to the same report/strategy result | not implemented | market-input replay only |

## Deep legacy-to-core capability comparison

The comparison is by behavior, not by legacy file or class identity.

| Legacy capability and semantics | Main parameters / result | engine_core equivalent | Status |
|---|---|---|---|
| trading-day decision and previous/next day | legacy accepts loose inputs and may fall back after a bounded search | immutable BaoStock-derived `TradingCalendarSnapshot`; strict date; fail closed | `INTENTIONAL_CHANGE`, implemented and tested |
| latest completed data day | current time plus caller completion cutoff (15:30 in the legacy path) | `latest_completed_trade_day(as_of, completion_cutoff_time)` | implemented; cutoff is readiness policy |
| runtime phase inference | wall time to PREMARKET/AUCTION/INTRADAY/LUNCH/POSTMARKET/NIGHT | versioned `SessionPlan` with aware instants and half-open intervals | implemented and tested |
| scheduled lifecycle actions | 09:25 finalize, 09:26 follow-up, 15:05 close, 17:40 settlement; duplicate-minute token | `TimerSpec`/`due_timer_firings` only | scheduling primitive implemented; actions not migrated |
| startup readiness audit | dataset watermarks, caches, missing/structural/dead-symbol gaps, phase-specific action/readiness | none | not migrated |
| phase-aware gap repair | heavy sync before 09:00/postmarket, bounded repair near open, fast Kaipan/cache repairs | only PreviousDayStats fetch/guard | not migrated |
| late-start auction recovery | once per process/day, 09:30-15:00, 0926 replay, possible Redis writeback | none | not migrated; legacy retry-on-failure needs audit |
| current Redis Q2 cohort | active set + per-symbol hashes; cohort freshness and source-time range | `RedisQ2ProjectionAdapter` | implemented read-only; real path verified but upstream stale |
| minute price/amount windows | legacy strips symbols, may use wall clock, converts missing references to zero | explicit source epoch, strict symbol/unit, Missing and COUNTER_RESET reasons | `INTENTIONAL_CHANGE`, implemented and tested |
| auction facts | legacy full runtime paths and reports | adjacent P/M/RB/RA facts + fact-only shadow | minimal slice only |
| opening/intraday context | Q2 freshness, market breadth, themes, battle status, candidates, hypotheses | raw market state and minimal probe | not migrated |
| theme/leader/large-cap/extreme/yesterday-limit/style | multiple caches/connectors and strategy layers | no corresponding facts/data functions | not migrated |
| Redis/TD persistence and cache refresh | auction anchor, kline/factors/chip/DDE, session facts and reports | no writer/checkpoint | intentionally absent |
| notification/report | rendered console/HTML plus deduplicated SMTP/webhook | canonical JSON trace only | production report/effect not migrated |
| replay | historical request and Q2Frame fixture with selected effects skipped | Q2Frame and deterministic TD event-time input replay | market input only; no full lifecycle/report parity |

The current `engine_core` calendar is not a holiday-only helper. It is the single authority for
`is_trading_day`, `previous_trade_day`, `next_trade_day` and the date passed to a date-sensitive
DataFunction. This corrects the legacy `holidays.CN()` approximation and its silent fallback
dates; it does not yet schedule or execute startup synchronization.

## Decision

`engine_core` is a sound deterministic calculation skeleton, but it is not yet a production replacement. The immediate production defect is Rabbit backlog/live-source lag, not an engine_core calculation error. The next work must not add more generic framework. First expose bounded t1-v2 live stage counters and fix throughput outside trading hours with replay/fixture verification; then migrate one real prefetch + auction/opening fact path with truthful time availability and read-only shadow output.
