# Project Knowledge

- [OBSERVED] 2026-09-18 17:35 CST real Cobra-ion recovery shadow for `600519` ran the existing bounded morning shell with real Redis Q2 and TD read-only inputs. It produced startup, `AUCTION_0926`, and `OPENING_0932`; startup checkpoint traces were `STARTUP_0830/0900=RECOVERY_CATCHUP/PARTIAL/STALE`, opening preserved the stale-Q2 gate, and all safety counters were zero. Source-tree identity matched local. This is post-market recovery evidence, not normal-origin opening acceptance or Core replacement. Evidence: `docs/evidence/m1_live_recovery_shadow_20260918_1735.md`.

- [OBSERVED] 2026-09-18 real reference shadow on Cobra-ion read TD `daily_kline` for `2026-09-17`, Redis yesterday-limit pool (47 rows) and hot plates (50 rows). Redis HLEN/HSCAN was consistent and source connections worked, but all three `DataResult`s remained `UNAVAILABLE` because historical `available_at_ms` is unknown and the fixed probe cutoff preceded fetch completion; Q2 was `STALE` with 5224/5224 coverage. This confirms fail-closed prefetch timing, not provider failure. Evidence SHA-256=`7852f547d22c054d525fa56f10a5df7f32e1b1e04c277dc15857be3088bcd5d0`; next real test must prefetch before 09:20.

- [OBSERVED] 2026-09-18 real Cobra-ion Redis startup probe through the new read-only shadow path returned `5224/5224` Q2 symbols with coverage `1.0`, but source data was `STALE/BEST_EFFORT_STALE`; readiness correctly remained `PARTIAL` and the probe ran in `POSTMARKET` at 17:29 CST. Artifact SHA-256 is `a60b45b06488db67f5938285907f8b71eb9d172408a417583ab6b5b13e21dded`. This is real connectivity evidence, not opening-time acceptance. No production write, Rabbit action, restart, or effect occurred. Evidence: `docs/evidence/m1_startup_shadow_real_probe_20260918.md`.

- [VERIFIED] 2026-09-18 commit `b4c2bc4` wires `StartupCheckpointTraceV1` into the existing read-only morning-shadow startup evidence. `startup.json` now records due 08:30/09:00 business anchors and actual observation times; the existing 09:26/09:32 shadow path is unchanged. Local and Cobra-ion Python 3.12.3 isolated suites both pass `511`; compileall passes and changed-file SHA-256 values match. No production service, Rabbit ACK/publish, Redis/TD write, notification, or effect changed. Evidence: `docs/evidence/m1_startup_shadow_trace_20260918.md`.

- [VERIFIED] 2026-09-18 commit `b7c1c25` adds the pure `StartupCheckpointTraceV1` wheel for 08:30/09:00. It preserves business anchor time separately from readiness observation time, includes readiness/Q2 and timer-firing identity in the trace hash, and has no provider, repair, persistence, Rabbit, Redis/TD write, notification, or effect behavior. Local and Cobra-ion Python 3.12.3 isolated suites both pass `511`; compileall passes and changed-file SHA-256 values match. This is not production startup ownership; `engine-next` remains the owner. Evidence: `docs/evidence/m1_startup_checkpoint_trace_20260918.md`.

- [OBSERVED] 2026-09-18 M0 startup parity audit against deployed `engine-next@20260903_e272842` confirms Core `StartupReadiness`/`SessionRuntimeCoordinator` are pure evaluation wheels, not a replacement startup owner. Legacy still covers 08:30/09:00 checkpoints, daily kline/factor/chip/DDE gap classification, dated limit/hot/mapping readiness and bounded repair actions, while Core currently covers already-observed Q2/references and read-only 0926/0932 shadow dispatch. 0920/0924/0925 source freeze remains external to t1-v2. Evidence: `docs/evidence/m0_startup_parity_audit_20260918.md`.

- [VERIFIED] 2026-09-18 bounded real-source parity closes only the Redis Q2 raw `am` mapping: `am → auction_amount_yuan` is exact for 000001 (`4,333,500`) and 000002 (`696,600`) against the deployed legacy context probe. 600519 deliberately records `SOURCE_PRIORITY_DIVERGENCE` (`Redis Q2 am=14,312,200` vs legacy auction projection `14,271,787`), proving the old consumer can prefer a frozen auction projection over the current Q2 state. No plate/strategy or full projection parity is claimed. The test uses a frozen real probe extraction, no writes, and preserves source-time semantics. Evidence: `docs/evidence/legacy_q2_amount_mapping_20260918.md`.

- [OBSERVED] 2026-09-18 16:45 CST the exact deployed `engine-next` context probe read real Redis Q2 for bounded `000001/000002/600519` and executed the legacy pure plate-bucket consumer under `GuardRedis` with `guard_writes=[]`. With a simulated 09:26 cutoff, returned source timestamps were in the future; the old path reported `latest_quote_age_seconds=0` by clamping the future age. Core must retain fail-closed future-time rejection. Plate strings were mojibake and the legacy labels remain observation-only; no plate strategy parity is proven. Evidence: `docs/evidence/legacy_active_consumer_probe_20260918_1645.md`.

- [VERIFIED] 2026-09-18 Gate B numeric auction parity closed for the real 600519 `0920→0924` fixture: Core `AuctionFactShadow` matches the verified legacy pure helper for price delta `-2060`, amount delta `4407516`, resting bid delta `648770`, resting ask delta `-129960`, and pressure delta `778730`. Legacy direction/labels remain intentionally outside the fact-only migration. Local/Cobra suites remain `508` with compileall PASS. Evidence: `docs/evidence/gate_b_legacy_numeric_parity_20260918.md`.

- [VERIFIED] 2026-09-18 16:38 CST commit `bcb7f68` added the capability-local legacy report status parity test for every Core `FactStatus`: `READY→COMPLETE`, `PARTIAL→PARTIAL`, and `MISSING/INVALID/UNAVAILABLE→DATA_UNAVAILABLE`. Local and cobra-ion isolated suites both pass `507`; compileall passes; the exact real TD auction shadow for 600519 remains deterministic. This closes only the deterministic report-status boundary; full legacy email/plate/locked-order/delivery/strategy parity remains open. No production source, storage, Rabbit, or effect path changed. Evidence: `docs/evidence/legacy_report_status_parity_20260918.md`.

- [VERIFIED] 2026-09-18 16:29 CST commit `a78e5a9` extended the real TD Auction Engine shadow with the build-only `AuctionFactReportArtifact`. Cobra-ion read `auction_snapshot_v2` for `600519`, processed 6 signals and 3 strategy results, preserved `PARTIAL/FACT_ONLY`, and produced direct/Engine semantic equality plus report `PARTIAL` status. Report text contains objective amount/pressure/price facts only; no strategy conclusion or effect path is connected. Artifact SHA-256=`8d9bed2f4860ff211540031e43619ba0be6bb4d4979fc8fadef9988e0c10df7f`. Local/Cobra suites after the code change both pass `502`; compileall passes. Evidence: `docs/evidence/auction_engine_report_shadow_20260918_1629.md`.

- [VERIFIED] 2026-09-18 16:21 CST bounded real TD auction shadow on cobra-ion read `auction_snapshot_v2` for `600519` through the public Core Engine path: 6 signals, 3 strategy results, `engine_fact_status=PARTIAL`, `engine_fact_only=true`, and direct/Engine semantic hash equality (`3904bb7d4a331bdfff9e38c8d26f8d1871752954ec41c482f05bac5d3de9e276`). Source timestamps for 0920/0924/0925 were preserved. This proves same-input wheel/Engine parity at TD projection granularity, not Rabbit batch equivalence or production replacement. Artifact SHA-256=`e0b34ec6f5f8740a20b39acd73605253db806f220c2eddd08cba0451c14ed4ef`. Evidence: `docs/evidence/auction_engine_shadow_20260918_1621.md`.

- [WARN] 2026-09-18 16:18 CST M2-1 real Q2 live-admission audit on cobra-ion: the existing read-only Redis path returned `5224/5224` symbols with coverage `1.0`, but all `5224` were stale and the newest source lag was about `4826s`; `BEST_EFFORT_STALE` remained explicit and repeated Core probe hashes were deterministic. `REAL_REDIS_Q2_READ_PATH=PASS`, `Q2_FRESHNESS=BLOCKED`, `M2_LIVE_TEMPORAL_ADMISSION=BLOCKED`. Artifact SHA-256=`39631e949cb6b28b4906589a55e83a3dc2aeb55e7841cd7c4187d38977e5ee9e`. No production service, Redis/TD write, Rabbit path, or effect changed. Evidence: `docs/evidence/m2_q2_live_admission_20260918_1618.md`.

- [VERIFIED] 2026-09-18 commit `9dd529d` made the real previous-day limit-pool and feedback probes carry an explicit `HISTORICAL`/`REPLAY`/`LIVE` temporal mode and preserve `fetch_completed_at_ms`. Local and Cobra-ion Python 3.12.3 isolated suites both pass `502`; compileall passes. A real Cobra-ion `HISTORICAL` probe read Redis previous-pool rows and bounded TD 0925 rows, returned `previous_result_status=UNAVAILABLE` with `available_at_ms=null` and no feedback records, proving the historical guard still fails closed. The artifact SHA-256 is `420827aa25a802ae175172074c2bf146dfdc42217097666618d0d79451fbe258`. No production service, Redis/TD write, Rabbit path, or effect changed. Evidence: `docs/evidence/reference_temporal_mode_20260918.md`.

- [VERIFIED] 2026-09-18 commit `6a56388` connects `PreviousDayLimitStructureFact` to the build-only report artifact as an optional fact section. It exposes previous-session date/count/highest-board/distribution only; current return feedback, plate/locked-order/mapping parity, strategy and delivery remain outside scope. Local/Cobra suites both pass `487`; compileall passes and changed report/test hashes match. Evidence: `docs/evidence/previous_limit_report_projection_20260918.md`.

- [VERIFIED] 2026-09-18 commits `4ca5915`/`bfbfd85`/`fddb3e2` add, harden, and run `PreviousDayLimitStructureFact` through the real Redis probe. A real Cobra-ion Redis capture had 47 rows, HLEN/scan consistency, and zero decode errors; because historical `available_at_ms` is absent, runtime fact output remains `UNAVAILABLE` and does not leak rows. The board-height mapping is immutable after hashing. Local and Cobra-ion suites both pass `488`; compileall passes; the real runner artifact SHA is `5ae6704b48da85dbeb43ae6992d23d2b078c45ab41173082c99a8e022fb3c6d7`. This closes only previous-session structure, not current return feedback, plate/locked-order parity, or full report ownership. Evidence: `docs/evidence/previous_day_limit_structure_fact_20260918.md`.

- [VERIFIED] 2026-09-18 `ec5dd37` closed two A2 report lineage gaps: canonical `limit_up_seal_amount_yuan` is present in summary mappings, and report `evidence_hash` now includes summary evidence identity while `semantic_hash` remains business-only. Local suite is `482 passed`; compileall passes. No provider/runtime/effect path changed. Evidence: `docs/evidence/auction_report_lineage_fix_20260918.md`.

- [VERIFIED] 2026-09-18 `adaf831` extends the narrow build-only `AuctionFactReportArtifact` with an optional normalized `AuctionMarketSummaryFact`. Summary semantic identity includes the normalized A2 fields while source/evidence identity stays in provenance; text remains fact-only and no delivery/effect path is connected. Local and Cobra-ion isolated suites both pass `481`; compileall passes. This closes A2 summary report projection only; plate rows, locked orders, mapping, delivery lifecycle, and strategy parity remain open. Evidence: `docs/evidence/auction_report_a2_integration_20260918.md`.

- [VERIFIED] 2026-09-18 A2 0925/auction summary fact wheel added in commit `a05da4c`: real captured summary fields are normalized to explicit count/yuan canonical fields; missing/invalid/explicit-zero behavior is covered. Local and cobra-ion suites both pass `480`; compileall passes. The frozen fixture is a small extraction of real capture evidence, not fabricated market rows. This closes summary normalization only, not live freshness or full report parity. Evidence: `docs/evidence/auction_market_summary_fact_20260918.md`.

- [VERIFIED] 2026-09-18 Core added a narrow build-only `AuctionFactReportArtifact` projection from frozen `AuctionFactShadow` (commit `69f8535`). It preserves explicit origin/status, missing-vs-zero, semantic/evidence hash separation, and provenance without Redis/TD/network reads, recovery, claims, SMTP, webhook, or strategy conclusions. Local and cobra-ion isolated suites both pass `474`; compileall passes. This does not make Core the production report/effect owner. Evidence: `docs/evidence/build_only_report_projection_20260918.md`.

- [WARN] 2026-09-18 14:47 Cobra-ion real Redis Q2 read-only probe: `5224/5224`, coverage=`1.0`, but all `5224` symbols are `STALE/BEST_EFFORT_STALE`; newest source lag about `1811s`. Repeated Core observation hash was deterministic, and the probe used only one `SMEMBERS` plus 5224 `HGETALL` operations. Artifact SHA-256=`9f98da4eb83d4c1965a37eb6d062d5b97b1dc786558815b10c35d67eb8e758d9`. `REAL_REDIS_Q2_READ_PATH=PASS`, `LIVE_Q2_FRESHNESS=WARN`; production replacement remains blocked by upstream lag/TD storage errors. Evidence: `docs/evidence/real_live_q2_20260918_1447.md`.

- [OBSERVED] 2026-09-18 14:47 legacy reporting boundary audit completed against the deployed `engine-next@20260903_e272842` release. Fact assembly, deterministic report build, delivery lifecycle/claim, notifier side effects, and strategy-console responsibilities are recorded separately. The narrow Core `AuctionFactReportArtifact` now covers only frozen single-fact status/origin/hash projection; A2 summary, plate rows, locked orders, mapping, lifecycle, and delivery parity remain open. `ENGINE_CORE_REPORT_OWNER=NOT_READY`. Evidence: `docs/evidence/legacy_reporting_contract_audit_20260918.md`.

- [WARN] 2026-09-18 14:34–14:38 Cobra-ion runtime/storage audit: `t1-v2-live` remains active and continues consuming/ACKing, but repeatedly reports `commit.tdengine: No enough disk space`; progress wall lag is roughly 26–27 minutes. A fresh read-only Core Q2 observation returned `5224/5224`, coverage=`1.0`, but all `5224` symbols are `STALE/BEST_EFFORT_STALE`, newest source lag about `1615s`, deterministic repeat hash PASS. Root is `94%` used with only `1.2G` free; active `infra_tdengine-data` is about `6.1G`, so no deletion/prune was attempted. Evidence: `docs/evidence/runtime_storage_lag_20260918_1438.md`.

- [VERIFIED] 2026-09-18 14:28 盘中 Cobra-ion 只读复验：真实 Redis Q2 返回 `5224/5224`、coverage=`1.0`，但全量为 `STALE/BEST_EFFORT_STALE`；重复 Core 观测 hash 一致。显式一分钟节点 cutoff 下，TD previous-day stats 与 Redis 昨日涨停池（47 行）为 `READY`，热板因 `hot/strength/net_inflow_yi` 单位未闭环保持 `UNAVAILABLE`；`temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`、整体 `PARTIAL`。服务 active/零重启、无写入/Rabbit ACK/effect。证据：`docs/evidence/real_live_reference_readiness_20260918_1429.md`。

- [VERIFIED] 2026-09-18 `turnover_yuan` canonical unit guard 在 Cobra-ion 真实旁路复验通过：469 tests、compileall PASS；previous-day stats/limit pool 均 READY（limit pool 47 行），hot plates 仍 UNAVAILABLE，Q2 5224/5224 STALE，整体 PARTIAL。复验 artifact SHA-256=`2957af09e890edda895f575e39b3f019fd6a3317edf30fef94f8a3f807a8e8de`。

- [VERIFIED] 2026-09-18 turnover canonicalization 在 Cobra-ion 隔离副本通过真实运行：TD `previous_day_stats=READY`，Redis `previous_day_limit_pool=READY`（47 行），热点板块继续 `UNAVAILABLE`（单位未闭环），Q2 `5224/5224` 但 `STALE`，整体 readiness=`PARTIAL`；`temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`。artifact SHA-256=`6ed6e2f5fe2b967367dd9b033d8e94f489f3e4c2ff35b29b126153355a19735b`，生产服务 active/零重启/零写入。详见 `docs/evidence/real_live_reference_turnover_20260918.md`。

- [VERIFIED] 2026-09-18 Legacy reference semantics 只读复核：旧 `StockAnalyzer.get_history_bans_pool()` 的 `rec[9]` 与真实 Redis `cache:yest_limit_pool:2026-09-17` 样本均为金额型 turnover，Core Provider 已将其 canonicalize 为 `turnover_yuan`；`close_pct` 保持百分比点。热点板块 tuple 路径 `tuple[6]/1e8 -> net_inflow_yi` 有 source-formula 证据，但 dict 路径不做同样换算，且当前 `strength/hot` 约 89–153 与旧 consumer `strength>=3000` 阈值量纲冲突，因此相关字段继续 UNKNOWN/NOT_AUTHORIZED。Redis metadata 仍无 `available_at_ms/field_units`，因此历史回放可用性仍未关闭。详见 `docs/evidence/legacy_reference_semantics_20260918.md`。

- [VERIFIED] 2026-09-18 13:49–13:55 真实 LIVE reference readiness 合同已在 Cobra-ion 隔离副本验证：Provider 记录实际 `fetch_completed_at_ms`；HISTORICAL/REPLAY 仍拒绝未知 `available_at_ms`，LIVE 仅在完成时间不晚于显式预取 cutoff 时放行。首轮使用读取起点 cutoff 正确失败闭环；显式一分钟 cutoff 重跑后 `temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`，TD `previous_day_stats=READY`，Redis 昨日涨停池/热板因单位 UNKNOWN 保持 `UNAVAILABLE`，整体 readiness=`PARTIAL`。本地/Cobra 同一代码均 `468 passed`、`compileall PASS`，未触碰生产服务或写入任何存储。详见 `docs/evidence/real_live_reference_readiness_20260918_1355.md`。
- [VERIFIED] 同一 live shadow 的参考预取现在用实际时钟记录 fetch completion，并把 09:26 作为显式节点 cutoff；晚启动不会把启动时间伪装成数据完成时间，超过节点 cutoff 的读取继续 fail-closed。

- [VERIFIED] 2026-09-18 13:12–13:18 Cobra-ion 真实只读链路复核：Redis Q2 返回 5224/5224、coverage=1.0，但 5224 条全部 stale（最新源时间落后约 6691 秒）；旧 `engine-next@20260903_e272842` auction loader 读取 0920/0924/0925 各 200 条 Top-200 投影，context probe 读取 3 个 bounded symbol，均 `guard_writes=[]`。Core public Engine opening/auction shadow 可重复执行；opening 保持 `STALE/PARTIAL/OBSERVE`，auction direct/engine fact hash 一致但仍 `FACT_ONLY/PARTIAL`。这是真实数据读路径与事实层证据，不是 Core 替代生产 owner。详见 `docs/evidence/real_readonly_chain_20260918_1315.md`。
- [VERIFIED] 2026-09-18 BaoStock 只读日历 probe 在 Cobra-ion 查询 `2025-12-01..2026-09-18` 成功，声明覆盖到 `2026-09-18`，197 个交易日；不把未来未发布日期伪造进快照。calendar semantic hash=`8f2a56c8dca12d7a37779fb14961ab5fb0bed21d03ef4aaf3a4c7b8d76c3b96f`。一次第三方参考源探针超出命令预算，其结果未采纳；第三方 I/O 仍必须在 reducer 外有界执行。
- [VERIFIED] 2026-09-18 13:23 真实 reference readiness：日历唯一推导 `2026-09-17`；TD daily-kline、Redis 昨日涨停池 47 行、Redis 热板 50 行均可只读取得且 Redis HLEN/HSCAN 一致，但三者均缺可证明历史 `available_at_ms`，Core 正确返回 `UNAVAILABLE/available_at_unknown`，整体 readiness=`PARTIAL`。这关闭真实连接/date authority/fail-closed 证据，不关闭 M2 runtime readiness。详见 `docs/evidence/real_reference_readiness_20260918_1323.md`。

- [VERIFIED] 2026-09-16 Core now has a minimal `AuctionReferencePreparationV1` composition: Calendar uniquely derives the previous trade date and the existing `PreviousDayStatsFunction`, `PreviousDayLimitPoolFunction`, and `HotPlatesFunction` run in fixed order outside the reducer. Exact cobra-ion verification of `8cff623` passed 430 tests. A real read-only run observed Q2 5221/5221 with 18 stale (`PARTIAL`), 32 previous-day limit rows and 50 current hot plates whose old metadata lacks verified availability/units (`UNAVAILABLE`), and zero TD daily-kline rows for the three bounded symbols (`MISSING`). Overall readiness correctly remained `PARTIAL`; no cache repair, fallback, write, Rabbit change, or effect occurred. See `docs/evidence/real_auction_reference_readiness_20260916_8cff623.md`.

- [VERIFIED] Verification baseline `528f5d0` was archived and verified on cobra-ion with matching archive SHA-256 `f071934879ea4245b72fdaf45cea8945da6689ad5fa6e09ff76b10677c96851d`: local and remote Python 3.12.3-compatible suites both pass `424`, compileall passes, and production `engine-next`/`t1-v2-live` remain active with zero restarts. The self-owned 2026-09-17 shadow schedule now points at latest archive `engine-core-498ff09`; see `docs/evidence/cobra_exact_verification_20260916_528f5d0.md`.

- [VERIFIED] 2026-09-16 12:42 CST the exact `2bbb1ae` shadow runner completed a real late-start read-only run. Both Core nodes performed one Q2 readiness read and observed `PARTIAL` with 1.0 row coverage but stale source data; `AUCTION_0926` retained its independent TD-owned `AnchorDeltaFactV1 READY` path and `OPENING_0932` produced `OpeningTransitionFactV1 READY`. The run was `RECOVERY_CATCHUP`, with no Rabbit/ACK/Redis/TD/effect side effects. See `docs/evidence/live_node_readiness_shadow_20260916_1242.md`.

- [VERIFIED] 2026-09-16 12:38 CST the exact `c4b0ac9` shadow runner performed a real late-start read-only run on cobra-ion. Each Core node rechecked Redis Q2 once: 5221-symbol coverage was 1.0 but readiness remained `PARTIAL`/`REFRESH_Q2` under the explicit 60-second freshness policy. `AUCTION_0926` retained its independent TD fact path (`AnchorDeltaFactV1 READY`) and `OPENING_0932` produced `OpeningTransitionFactV1 READY`; both were `RECOVERY_CATCHUP`, with no Rabbit/ACK/Redis/TD/effect side effects. See `docs/evidence/live_node_readiness_shadow_20260916_1238.md`.

- [VERIFIED] 2026-09-16 Core live shadow now performs one bounded, read-only Q2 readiness recheck at each Core-owned node. `AUCTION_0926` keeps its TD-owned fact path when the Q2 probe fails and records the probe error; `OPENING_0932` preserves fail-closed behavior because it requires Q2. The node readiness assessment is evidence only and does not alter TD fact semantic hashes. Local and Cobra-ion Python 3.12.3 exact archive for `a8d9a31` both pass `424` tests and compileall; production services remain active with zero restarts. See `docs/evidence/engine_node_readiness_20260916_a8d9a31.md`.

- [VERIFIED] 2026-09-16 12:45 CST the self-owned normal-origin Core morning shadow for 2026-09-17 was replaced with latest exact archive `/home/exedev/validation/engine-core-498ff09` (PID 525000, explicit build id `498ff09`). It waits for absolute Asia/Shanghai 09:14:50, uses a real BaoStock calendar probe and bounded symbols `000001,000002,600519`, and writes once under `/home/exedev/validation/live-morning-shadow-20260917-0915`; the earlier wait shell was stopped, while production services remain owners and no Rabbit/ACK/Redis/TD/effect path is changed. See `docs/evidence/scheduled_live_morning_shadow_20260917.md`.

- [VERIFIED] Final evidence HEAD `bcfcbf4` was archived and tested on cobra-ion with the production Python 3.12.3 environment: identical archive SHA-256 `6cbab641776e76c54324fa0bd897f124057c5370b1f30ca86802201af73cf9d8`, 421 tests passed, compileall passed, and source build identity remained `f8c88da7d633d27b57fd2249496d17f4d734c3cc6b31dfcbf749c5ed6cc59db05`. Both production services stayed active with zero restarts and no side effects. See `docs/evidence/cobra_exact_verification_20260916_bcfcbf4.md`.

- [VERIFIED] 2026-09-16 exact archive `ba309b8` (SHA-256 `af4ed8ce1ee0066ea2665c94353a59402334d7882c74d17f14152394a6d15cd8`) passed the full 421-test suite and compileall on cobra-ion Python 3.12.3, matching local results and source-tree build identity `f8c88da7d633d27b57fd2249496d17f4d734c3cc6b31dfcb749c5ed6cc59db05`. The same exact build performed real read-only Redis Q2/TD probes: Q2 5221/5221, coverage 1.0 but `PARTIAL` with all symbols stale under 60s; TD 600519 auction fact-only Engine shadow kept direct/engine hash equal; previous-day request 2026-09-16 derived 2026-09-15 and returned `MISSING` with zero rows. Production services remained active with zero restarts and no writes/effects. See `docs/evidence/cobra_exact_verification_20260916_ba309b8.md` and `docs/evidence/real_core_shadow_20260916_ba309b8.md`.

- [VERIFIED] 2026-09-16 normal-origin Core morning shadow ran on cobra-ion before the session and emitted `AUCTION_0926`/`OPENING_0932` at the actual due times with zero Rabbit/Redis/TD/notification side effects. Startup at 09:14:50 correctly reported Q2 `MISSING/EMPTY_UNIVERSE` and `WAIT_FOR_Q2`; by 09:32 Q2 was 5221/5221 with coverage 1.0 but batch status `PARTIAL/BEST_EFFORT_PARTIAL`, and bounded AnchorDelta/Opening fact dispatch completed for three symbols. The runner did not record an immutable code commit, so this is real normal-origin lifecycle evidence but not exact-current-HEAD verification; full Core replacement remains not ready. See `docs/evidence/live_morning_shadow_20260916_0915.md`.

- [VERIFIED] 2026-09-15 11:08 Cobra-ion scheduled the next normal-origin live morning shadow: detached PID `233578` will start before 09:15 on `2026-09-16`, use the existing read-only runner and calendar, capture `AUCTION_0926`/`OPENING_0932` through 09:33, and write only to `/home/exedev/validation/live-morning-shadow-20260916-0915`. The target was absent and the process was alive at scheduling verification; production services and side-effect boundaries remain unchanged. See `docs/evidence/scheduled_live_morning_shadow_20260916.md`.

- [VERIFIED] 2026-09-15 11:13 Cobra-ion real `PreviousDayStatsFunction` read: `DataRequest.trade_date=2026-09-15` was validated as a trading day and the single calendar authority derived `previous_trade_date=2026-09-14`. The verified TD daily-kline path returned zero rows, so Core reported `MISSING`, `completeness=0.0`, and `available_at_ms=null`; no fallback date, repair, or write was attempted. This is correct fail-closed readiness evidence, not a provider connectivity failure. See `docs/evidence/real_reference_readiness_20260915_1115.md`.

- [VERIFIED] 2026-09-15 11:05–11:08 Cobra-ion real-source read-only probe: Redis Q2 returned 5220/5220 rows with all required fields and coverage `1.0`, but all symbols were `STALE` under the explicit 60s policy; newest source time lagged wall time by roughly 75 minutes. The same observation produced identical Core probe/snapshot hashes on two runs. TD `market_data1.stock_tick_v2` contained real rows through 09:51:55, while `auction_snapshot_v2` had no current-day rows. Individual Opening facts being field-level `available` does not override batch `projection_status=STALE`/`freshness_status=STALE_OR_MIXED`; consumers must preserve both levels. No Redis/TD writes, Rabbit consumption/ACK, notification, or restart occurred. This is real-data shadow evidence, not normal-origin 09:26/09:32 proof or Core replacement acceptance. See `docs/evidence/real_live_probe_20260915_1106.md`.

- [VERIFIED] 2026-09-14 21:34 Cobra-ion 真实 Provider 复验：TD `daily_kline` 按日历派生 `2026-09-11` 返回 600519/000001 两行；Redis 热板真实返回 50 行、昨日涨停池真实返回 40 行且 HSCAN/HLEN 一致。三者均因缺少可证明的历史 `available_at` 保持 `UNAVAILABLE(available_at_unknown)`；这证明真实连接、日期推导与 fail-closed 合同，不证明历史 runtime 可用或 Core replacement。详见 `docs/evidence/real_provider_probe_20260914_2134.md`。

- [VERIFIED] 2026-09-14 离线/跨平台验证基线更新（代码与测试提交 `12483d7`，当前文档头随后仅补证据）：本地与 Cobra-ion Python 3.12.3 同一归档均为 `404 passed`，`compileall` PASS；归档 SHA-256 为 `a9bfb150cde36cf155c1db9372d2392d8c7def2690ce9987fad11efc03a034aa`。新增日期绑定 Provider 边界测试只证明离线合同，不改变在线 Redis/TD/生产替代验收状态。此前 `322/393/397/400` 等数字保留为历史提交证据，不代表当前测试总数。详见 `docs/evidence/cobra_exact_verification_20260914_12483d7.md`。

- [VERIFIED] 2026-09-14 19:23 Cobra-ion 排程审计发现此前三个相对 `sleep` 会错过预定窗口；已在不触碰 `engine-next`/`t1-v2-live` 的前提下，仅终止自有等待 shell 并按服务器绝对目标时间重排。当前 PID：capture `51820`（09:09:40）、Core morning shadow `51823`（09:15:00–09:33）、post-capture audit `51826`（09:40）；均使用独立验证目录，输出目录启动前为空。详见 `docs/evidence/scheduled_readonly_capture_20260915.md`。

- [VERIFIED] 2026-09-14 修复明日 Morning Shadow 的日历加载阻塞：远端 `cc-m0-calendar-20260911-v3.json` 是 `RealCalendarProbeV1` 原始证据格式，`6baba2d` loader 现在兼容该格式和标准日历 fixture，按显式 query guard bounds 重建快照并校验 semantic hash。Cobra-ion Python 3.12.3 对精确归档为 `395 passed`、`compileall PASS`；自有只读 Shadow 已切换到修复副本 `engine-core-6baba2d`，生产服务未改动。详见 `docs/evidence/calendar_probe_loader_fix_20260914.md`。

- [WARN] 2026-09-14 t1-v2 runtime lag：只读日志样本中 `wall_lag_ms` 从 09:25 的 `2418` 增至 09:30 的 `12222`，15:23 达到 `1428792`（约 23.8 分钟），尽管采样行 `ack_fail=0` 且服务 active。该问题根因 UNKNOWN，必须纳入 Core 替代前多交易日容量/积压门槛；未授权盘中修改 producer。详见 `docs/evidence/runtime_lag_observation_20260914.md`。

- [WARN] 2026-09-14 同一 t1-v2 进度行存在累计 `source_reject`（09:25 `3,674,985`，15:23 `5,494,355`）和少量 `last_reject`；日志没有给出拒绝分支/字段原因，不能把它解释成 Rabbit 丢失或 ACK 失败。该未知已加入 Core 替代前的输入完整性门槛，未修改 producer。详见 `docs/evidence/runtime_input_reject_observation_20260914.md`。

- [VERIFIED] 2026-09-14 18:30 Cobra-ion 真实 Provider 复验：Redis Q2 5220/5220、coverage=1.0 但全量 `STALE`；TD PreviousDayStats 派生 `2026-09-11` 并返回 3 行，但 `available_at_ms` 未知，runtime 结果为 `UNAVAILABLE`；Redis/TD 竞价对照 `mismatch=0`，整体仍为 `PARTIAL_COMPARABLE/NOT_COMPARABLE`。所有操作只读，无生产副作用。详见 `docs/evidence/real_provider_probe_20260914_1830.md`。

- [VERIFIED] 2026-09-14 测试范围审计：46 个测试模块、319 个静态测试函数、pytest 参数化后 393 个用例；本地与 Cobra 均通过。测试套件主要覆盖离线轮子和 fake 客户端，真实 Redis/TD/第三方连接由显式 Cobra 只读 probe 单独证明；in-session 生产 Shadow、批边界和替代验收仍未完成。详见 `docs/evidence/test_scope_audit_20260914.md`。

- [VERIFIED] 当前提交 `cf90956` 的固定归档在本地与 Cobra-ion Python 3.12.3 复验一致：两端 `393 passed`、`compileall` PASS，归档 SHA-256 `9eb57034d83e1f52f17bb90ac6879bde2b9576d1f06863cb55c89c85fbaacdd8`。归档目录不含 `.git`，`git diff --check` 仅在本地提交前执行；该证据不改变生产服务或 Core replacement 状态。详见 `docs/evidence/verification_cf90956_20260914.md`。

- [OBSERVED] 2026-09-14 09:24:50–09:26:30 生产日志只读切片：t1-v2 在 09:24:51/09:25:02 继续处理并 ACK 批次（`ack_fail=0`），保留 `last_ts_ms` 与 wall lag；engine-next 记录 09:25:10 `auction_finalize_0925`、09:25:49 完成 5219 条 context、09:26:27 `auction_followup_0926` 并由生产 owner 发出邮件。日志没有 exact Rabbit batch、AuctionState commit 或 writer 同源标识，因此 batch/freeze/projection parity 仍 UNKNOWN/UNPROVEN。详见 `docs/evidence/runtime_log_slice_20260914_0925.md`。

- [VERIFIED] 2026-09-14 18:06 Cobra-ion 真实参考源探针：复用 `engine-next@20260903_e272842` 的 Baostock/Kaipanla/Wencai/THS 连接方式，6/6 调用成功。Baostock 日线请求与返回交易日均为 `2026-09-11`；其他源只证明当前响应可读，日期/历史 `available_at` 仍 UNKNOWN，不能进入 Replay runtime。乱码响应按原样保留，不冒充语义修复。artifact SHA-256 `ff8d875b9a8a1d50134280be0160df99085c822c45c2a6d6fcfffd63210f5bbd`。详见 `docs/evidence/real_reference_probe_20260914_1806.md`。

- [VERIFIED] 2026-09-14 盘后独立 read-only probe：对 Cobra 当前 `engine-next@20260903_e272842` 的 `load_auction_snapshots()` 与旧 context/fact path 分别执行真实 Redis 读取，三只 bounded symbol、无 GuardRedis 写入。loader 当前观察到 0920/0924/0925 各 200 条 Top-200 projection（artifact `033536e5efbfe810575dc350e291c84e3016d39c8d19b6f833aee8b6a2e28ebd`），context probe 读取 3 行（artifact `36e44f3ada5c74d755c87613ab6922406cf0bb84b941481b68135f25d835c3cc`）。两者是不同 projection，未共享 immutable AuctionState/source-time，不宣称 parity 或 first divergence；Rabbit batch、freeze membership 与正式 effect 仍 UNKNOWN。详见 `docs/evidence/engine_next_loader_context_probe_20260914_1752.md`。

- [VERIFIED] 2026-09-14 生产链审计工具收口：`7f24d33` 对非空输出目录 fail-closed，防止证据重跑覆盖或混入旧文件；Cobra-ion Python 3.12.3 同归档通过 `393 passed`、`compileall`。该修正不改变六层矩阵或 Core 事实，仅强化证据 write-once 边界。

- [VERIFIED] 2026-09-14 六层生产链捕获审计：当前提交 `cb5d6fd` 在 Cobra-ion Python 3.12.3 使用真实 `20260914` capture、真实 TD `auction_snapshot_v2` 600519 源行和 09:24:50–09:30:01 TD Tick 样本生成六层 `production_chain_matrix.csv`、tick morphology 与 `audit_summary.json`。Q2 5220/5220、coverage=1.0 但 `STALE/BEST_EFFORT_STALE`，Core Q2 重复 hash 一致；auction fact 为 `OBSERVED/FACT_ONLY/PARTIAL`。0924 capture 槽位仍 `MISSING`，未用后续数据补写；Gateway/Rabbit batch、内部 AuctionState/freeze、engine-next loader trace 仍 UNKNOWN。审计工具已修正独立 TD fact 的矩阵归属：缺失 0924 和 aggregate anchor 行为 `UNPROVEN`，不冒充 capture 观察。接受结论 `SOURCE=UNKNOWN/AUCTION=OBSERVED/STORAGE=WARN/ENGINE_NEXT=UNKNOWN/CORE=PARTIAL/JOINT=WARN`，安全计数全 0。证据：`docs/evidence/production_chain_shadow_20260914_full.md`。

- [VERIFIED] 2026-09-14 `LiveMorningShadowV1` 最新提交 `472bf7d` 在 Cobra-ion Python 3.12.3 同归档通过 `391 passed`/`compileall`；17:28 晚启动真实只读运行生成 startup、`AUCTION_0926`、`OPENING_0932` 三份证据，origin=`RECOVERY_CATCHUP`，节点使用实际 17:28 observation time，Q2 trace 保留 `STALE/BEST_EFFORT_STALE` 与 source-time range，GuardRedis/TD SELECT/Redis Q2 均无写入，安全计数全 0。该结果证明 Core 旁路可在真实连接上安全晚启动，不证明盘中 09:26/09:32 时点；下一交易日须 09:15 前启动。归档 SHA-256 `0cb91fd65fab3cdc251558c74f284e803a265c7691f0c834ae42487915faecf7`。详见 `docs/evidence/live_morning_shadow_20260914_1728.md`。

- [VERIFIED] 2026-09-14 新增 `run_live_morning_shadow.py`（Core commit `44d6a46`）：有界只读运行壳在节点消费时刻调用现有 SessionTimer、Redis Q2、TD auction reader、GuardRedis legacy auction loader 与既有 morning fact dispatch；不新增 scheduler、Rabbit consumer/ACK、Redis/TD writer、通知或 effect，输出目录 write-once。Cobra-ion Python 3.12.3 同归档为 `390 passed`、`compileall` PASS；17:17 晚启动实跑生成 `startup.json`、`AUCTION_0926.json`、`OPENING_0932.json`，origin=`RECOVERY_CATCHUP`，safety 全 0。节点使用实际 17:17 观察时间而非伪造 09:26/09:32，故该证据不替代下一交易日盘中时点捕获。证据：`tmp/live-morning-shadow-20260914-1717/`。

- [VERIFIED] 2026-09-14 对当前 Cobra release 的 `IntradayDataHub.load_auction_snapshots()` 做只读探针：Redis 当前 `0920/0924/0925` 各返回 200 行 TopN 投影，600519 三个锚点均存在，000001/000002 不在这三个 TopN 结果中；`guard_writes=[]`，artifact SHA-256 `2dbdb285aef39e689c1d78838ca5dc256c2b337065e522d7c349ae405b1f5043`。同一投影映射到 Core 后仍为 `FACT_ONLY/OBSERVE`，0920/0925 price=0 保持语义 UNKNOWN；该结果只证明当前时点投影可读，不证明完整市场快照、Rabbit batch 或 Redis/TD writer 上游一致。详见 `docs/evidence/legacy_auction_loader_probe_20260914.md`。

- [VERIFIED] 2026-09-14 新增 `StartupReadinessV1` 只读自检轮子（commit `4e7b2f1`）：显式校验 Calendar/SessionPlan、Q2 source-time cutoff、已获得的参考数据可用性和 SessionTimer 到期节点；缺 Q2 时只返回 `WAIT/DEFER`，不调用 Provider、不预取、不写 Redis/TD、不接 Rabbit。Local/Cobra 3.12.3 同一归档 `386 passed`，真实 Cobra Redis Q2 probe 为 `5220/5220`、coverage `1.0` 但 `STALE`，artifact SHA-256 `575d9e5c152a39c023521210872f2824c335634915d71045df4888db20b711f5`。这不是 Core 替代 engine-next 的启动协调或生产部署。详见 `docs/evidence/startup_readiness_probe_20260914.md`。

- [OBSERVED] 2026-09-14 cobra-ion 只读对照探查：旧 `engine_next` context builder 与 Core `RedisQ2ProjectionAdapter` 均可在 Guard/只读边界运行且无写入；旧链对指定 09:26 诊断读到晚于该时间的当前 Q2，并将未来源年龄裁为零（`future_source_timestamp=true`），因此不适合作为历史 replay oracle。Core 同日读取 5220/5220、coverage=1.0 但 `STALE/BEST_EFFORT_STALE`，同一观察重复运行确定性一致；两链未共享不可变输入快照，跨链路 exact parity 保持 `UNPROVEN`。证据见 `docs/evidence/legacy_core_context_probe_20260914.md`。

- [VERIFIED] 2026-09-14 Gate B morning fact dispatch：最终 Core commit `244e7ae`（状态修正 `b241a70`、typed-unit 修正 `ca05ddf`、功能提交 `41bc60d`、边界修正 `d976f6d`）在薄组合边界中派发 `AUCTION_0926`→`AnchorDeltaFactV1`、`OPENING_0932`→`OpeningFactV1/OpeningTransitionFactV1`，拒绝跨股票/重复 auction tag，并按生产 typed TD `chg_bp/100` 合同转换竞价变动。Cobra-ion Python 3.12.3 同归档通过 `373 passed`、`compileall`；真实 Q2 capture 5220/5220 但 `PARTIAL`，100 只 opening helper exact，可比 transition 99/99 exact，1 只真实空字段为 `NON_COMPARABLE`，产物含原因计数。详见 `docs/evidence/gate_b_morning_fact_dispatch_20260914.md`。

- [VERIFIED] 2026-09-14 新增只读 `morning_vertical_slice_shadow` 组合工具（Core commit `b902d7e`）：真实 Redis Q2 + TD `auction_snapshot_v2` + SessionPlan timer evidence 可在不启动完整 Engine、不执行策略、不写 Redis/TD 的条件下生成可追溯 morning shadow。固定输入在 Cobra-ion Python 3.12.3 重跑两次 artifact/semantic hash 一致；本地/Cobra 同一归档均为 `332 passed`、`compileall` PASS。真实结果保持 Q2 `coverage=1.0` 但 `STALE`，auction `PARTIAL/FACT_ONLY/OBSERVE`，reference data `UNAVAILABLE`；这不是 engine-next 替代证明。详见 `docs/evidence/morning_vertical_slice_shadow_20260914.md`。

- [VERIFIED] 2026-09-14 Gate B 首条事实迁移为 `AnchorDeltaFactV1`（最终 Core commit `697b6ba`）：只迁移旧生产纯函数的逐股 0920→0924、0924→0925 anchor delta。Cobra-ion 同一真实 TD 输入与 `engine_next@e272842` 逐字段 canonical JSON exact match；当前日 price 缺失时两边均 fail-closed 为 `unavailable`。该事实轮子不包含板块评分、策略阈值或报告 effect，不能解释为 AuctionStrategy 已迁移。最终本地/Cobra 套件 `351 passed`。详见 `docs/evidence/gate_b_anchor_delta_20260914.md`。

- [VERIFIED] 2026-09-14 cobra-ion 真实只读 readiness 复核：`cache:hot_plates:2026-09-14` 为 50 行 hash、`cache:yest_limit_pool:2026-09-11` 为 40 行 hash，扫描与 HLEN 一致；两者 metadata 均缺 `schema_version/available_at_ms/field_units`，core 分别保持 `UNAVAILABLE(available_at_unknown)`，昨日涨停池另保留 `turnover_unit_unknown`。同次 Q2 读取 5220/5220、coverage=1.0，但在 60s freshness policy 下 5220 条均为 `STALE/BEST_EFFORT_STALE`，不能解释为 fresh。所有命令仅 `SMEMBERS/HGETALL/TYPE/HLEN/HSCAN/GET`，未写 Redis/TD、未改服务；完整 hash/证据见 `docs/evidence/reference_data_readiness_20260914.md`。
- [VERIFIED] 2026-09-14 M0 current-release 启动/动作链只读审计：Cobra 部署路径为 `releases/20260903_e272842`（无 Git 元数据），`engine-next`/`t1-v2-live` 均 active 且零重启；已记录 systemd 入口、08:30/09:00、09:20/09:24/09:25/09:26/09:32 节点、getter/action 读写边界及 source/runtime 证据。当前 Core 只读 Q2 可运行，但启动协调器尚未实现；`recover_auction_anchor`、hot/yest fetch、mapping loader、market summary rebuild 均可能写入或触发外部 I/O，不能直接接入 Core。详见 `docs/evidence/m0_startup_flow_audit_20260914.md`。
- [VERIFIED] 2026-09-14 Core 仅补充纯启动/重启矩阵测试：复用 `SessionPlanV1` 与 `SessionTimerV1` 计算 Core-owned `AUCTION_0926`/`OPENING_0932` 的正常与 `RECOVERY_CATCHUP` due nodes；09:20/09:24/09:25 source freeze 仍由 t1-v2 负责。非交易日、跨日期、重复/未知完成身份均 fail-closed。该测试不实现 StartupReadiness、scheduler、持久化或跨重启 exactly-once。详见 `docs/evidence/m1_startup_restart_matrix_20260914.md`。
- [VERIFIED] 2026-09-14 `ad098c5` 检查点的 Cobra 精确归档校验已修正一次归档布局错误：`git archive` 本身保留根路径，不能额外使用 `--strip-components=1`；在全新目录按原路径展开后，Cobra Python 3.12.3 通过 `322 passed`、`compileall`，归档 SHA 为 `e05e7ae53555f0259fcf1294a377871f756b654e5b382450f741679346d64ec9`。详见 `docs/evidence/cobra_exact_verification_20260914_current.md`。
- [VERIFIED] 2026-09-14 当前文档收口 HEAD `b7ae956` 也已在 Cobra Python 3.12.3 的全新目录按原路径展开并通过 `322 passed`、`compileall`；归档 SHA 为 `c9874994b9bc991de2ddea9208943496054896504122a393156a7d5adf26e4c5`。该验证仍是隔离测试，不代表 Core 已部署或替换 `engine-next`。

## 0. Current execution status (2026-09-13)

### 2026-09-14 current verification override

- [VERIFIED] 2026-09-14 交易日真实生产链只读旁路：`engine-next` PID 4022407 与 `t1-v2-live` PID 2878024 均 active、NRestarts=0；未新增 Rabbit consumer、未改变 ACK、未写 Redis/TD、未发送通知。真实 capture 副本已保存到 `tmp/capture-20260914/`，关键 0920/0925/anchor 文件与远端 SHA-256 一致。
- [VERIFIED] 2026-09-14 capture required slot `auction_0924` 在 09:24:05 捕获时为空；后续 Redis/TD 虽出现 0924 数据，也不能回填早先空槽位。本次 ground truth 重新计算为 `PARTIAL`，不得信任 manifest 中矛盾的 `formal_ground_truth=true`。
- [VERIFIED] 2026-09-14 真实 Q2 `q2_093210.jsonl` 为 5220/5220、coverage `1.0`，但 source-time range 为 2026-09-14 00:00:00 至 09:31:14，10 秒 freshness policy 下全部 `STALE/BEST_EFFORT_STALE`；捕获文件经过 Q2 Adapter + in-memory Engine 重复运行，Probe hash 一致。直接 Redis 探针在约 09:49 观察时同样为 5220/5220 但全部 stale。
- [VERIFIED] 2026-09-14 cobra-ion 只读 TD `daily_kline` 为日历派生的 2026-09-11，3 个 symbol 均有行，但 `available_at` 未知，`PreviousDayStatsFunction` 按合同返回 `UNAVAILABLE`，未把查询时间冒充历史可见时间。
- [VERIFIED] 2026-09-14 只读 TD `stock_tick_v2` 在 09:24:50–09:30:00 选定 3 个 symbol 返回 11 行；已保留五档可见性、价格/量额存在性和 source timestamp，未推断同毫秒真实顺序。真实 TD 竞价投影的 600519 0920/0924/0925 shadow 保持 `PARTIAL/FACT_ONLY/OBSERVE`。
- [VERIFIED] 新增 `examples/run_production_chain_shadow.py`：从已捕获真实文件生成六层 `production_chain_matrix.csv`、tick morphology 证据和 `audit_summary.json`，并以当前 Core Q2 Adapter/Engine 重复计算验证确定性；工具不连接生产服务、不执行修复、不写 Redis/TD。另可从真实 TD `auction_snapshot_v2` 源行重算 AuctionFactShadow，但不接受预计算结果自证。
- [VERIFIED] 当前只读生产链审计工具的可执行代码 commit 为 `cb1ea9d863ed6340b15e760e2dadc041ba74e07a`；cobra-ion Python 3.12.3 正式运行时执行 `320 passed`、`compileall` 通过，真实 capture/TD 样本生成的六类审计产物字节级 SHA-256 与本地兼容检查一致。Windows 当前仅有 Python 3.9.13，因 `pyproject.toml` 要求 `>=3.12`，其结果只作 informational compatibility check，不冒充同运行时验收。默认摘要将 `AuctionState` 降为 `OBSERVED`、存储投影降为 `WARN`，并将 `engine_core_q2_path=PASS` 与完整竞价 Shadow `PARTIAL` 分开；带真实 TD 源行时 `engine_core_auction_fact=OBSERVED`，仍不宣称 Engine-connected。此前的 `9392f1e`、`e106e12`、`0b6021c` 及更早测试/commit 记录均为历史证据，不代表当前代码身份；详见 `docs/production_chain_shadow_20260914.md`。
- [VERIFIED] Cobra-ion 2026-09-14 开盘前真实 Redis 只读探针连接成功，但 `q2:active:20260914` 为空，结果为 `MISSING/EMPTY_UNIVERSE`、coverage `0.0`；两次同一观察 Engine hash 一致。该结果只证明真实连接和 fail-closed，不证明 live Q2 正向覆盖。
- [OBSERVED] 2026-09-14 ground-truth capture 进程 `PID=4014185` 已启动并等待 09:20/09:24/09:25 采集点，当前尚无交易时段 artifact。`engine-next` 与 `t1-v2-live` 保持 active，Cobra 根分区约 83% 使用率、约 3.1GB 可用。
- [OBSERVED] Cobra-ion 既有 `t1_v2.log` 在 2026-09-11 09:19–09:25 持续记录 batches/source_in/ack/ticks/last_ts_ms，证明运行计数连续；日志没有 batch_id、`emit_a25` 或 writer 顺序，因此 `FINAL_TICK_BATCH_MEMBERSHIP` 仍 UNKNOWN。详见 `docs/evidence/t1_runtime_progress_20260911.md`。
- [VERIFIED] `DeterministicEngine._registered_evaluation_ids` 已改为有界 session ledger（默认 65536）；不驱逐已注册身份，达到上限时 fail-closed，避免终态 tombstone 淘汰后 evaluation 复活。Cobra-ion Python 3.12.3 对 commit `3d870ee` 通过 `375 passed`、`compileall`；跨进程/跨重启 durable identity 仍延期，不得解释为 replacement-ready。详见 `docs/evidence/engine_registration_bound_20260914.md`。

- [VERIFIED] 可执行 Core 代码对象为 `ed3547c5799e6b72dcdcc79f1f13d500616ac0fa`；本地 Windows 与 cobra-ion Python 3.12.3 临时归档均通过 `306 passed`、`compileall`。当前分支后续提交仅补充审计/证据文档，未改变 `src/` 或测试语义。
- [VERIFIED] 2026-09-13 对提交 `bc2b2c10c77a5ae0e1719a5b083d7bc426f401d6`（后续仅有文档提交）在 cobra-ion Python 3.12.3 临时目录重跑完整套件：`306 passed`、`compileall` PASS；同次只读真实 TD 竞价/昨日数据和既有第三方连接探针均执行成功，证据见 `docs/evidence/real_provider_cross_source_audit_20260913.md`。周日 Redis 没有 `q2:active:*`，不宣称 live Q2 可用。当前可执行代码提交为 `2375e6be8ab10a2d380597d60a38b9e5272d66f5`，之后提交仍仅补充审计文档。
- [VERIFIED] 旧 `web.services.tdengine_service.TDengineService` 初始化路径包含建库/建表检查，不能直接作为 Core 只读 Provider；当前 `TDPreviousDayStatsProvider` 只接收沿旧表/日期语义抽取的只读 callable，避免把 legacy 初始化副作用带入 Core。该边界说明已补入 `docs/evidence/legacy_connectivity_inventory_20260909.md`。
- [VERIFIED] 真实只读 TD 竞价影子已重建 600519 的 0920/0924/0925 三锚点：0920→0924 为 `PARTIAL`、0924→0925 为 `READY`，输出保持 `FACT_ONLY/OBSERVE`；Redis 同日期投影无可比较行，因此不得宣称 Redis/TD 等价。
- [VERIFIED] 真实只读 Redis opening probe 在空 `q2:active:20260910` 时输出 `MISSING/EMPTY_UNIVERSE`、coverage `0.0`、freshness `MISSING`；这证明缺失状态不再被误报为 `FRESH`，不证明历史 live coverage。
- [VERIFIED] 真实只读 TD `daily_kline` 为日历派生的 2026-09-09 返回 3 行，但因历史 `available_at` 未知，`PreviousDayStatsFunction` 正确输出 `UNAVAILABLE`；查询时刻仅作 `observed_at` 审计。
- [VERIFIED] Cobra `engine-next` 与 `t1-v2-live` 当前 active、零重启；`engine_core` 尚未部署为生产服务。根分区约 83% 使用率、可用约 3.1GB；本轮仅使用独立 `/tmp` 验证目录并清理。

## 1. Market & Auction Knowledge

- [UNKNOWN] Q2 generation 是否是旧生产链真实提供的全局一致版本，待 Gate K 证实。
- [OBSERVED] 旧 Q2 读取使用 q2:active:{trade_date} 与 q2:{symbol}；px/pc 为 milli 价格字段，其余数量单位必须继续审计。
- [INFERRED] Q2 是 Redis 中各股票最近状态的投影集合，不应默认解释为同一市场时刻的全市场快照。
- [VERIFIED] 2026-09-04 对 cobra-ion 的只读 Probe 可通过既有 server venv 访问 Redis Q2；当时最新可用 cohort 为 `q2:active:20260903`，共 5217 个 symbol。
- [OBSERVED] 2026-09-04 读取 `q2:active:20260904` 时覆盖完整；较早的 `q2:active:20260903` 中大多数记录的 source `ts` 已跨到 2026-09-04，active cohort 不能视为不可变历史快照。
- [OBSERVED] Q2 `ts` 与 cobra-ion 上同一时段 `stock_tick_v2.ts` 对齐，当前 canonical 名称为 `source_record_time_ms`；可以用于 freshness 和 trade-date sanity，但不能宣称为交易所逐笔时间或 Rabbit arrival time。上游供应商对该时间的更细定义仍 UNKNOWN。
- [VERIFIED] `C/t1_v2` producer 维护 `amt` 为累计元、`vol` 为累计手（board lots），`amt2m/amt5m` 为累计金额差；Redis/TD 写入保持这些整数单位。旧注释中的 shares 表述不作为新契约依据。
- [VERIFIED] 2026-09-09 真实 Q2 量纲交叉验证进一步关闭 `vol` 单位：`000001/300750/600519` 的 `amt/(vol*100)` 与当前价格比约为 `1.0015/1.0005/1.0036`，若按 shares 解释则约为当前价格 100 倍。该结论不自动扩展到未进入当前 Wheel 的 `iv`。
- [VERIFIED] cobra-ion 当前 `C/t1_v2` 竞价计算将 `br/ar` 定义为一级价格×二档手数换算的元金额，`am` 为竞价成交金额；它们是派生盘口/成交代理，不是真实净流入。
- [OBSERVED] Q2 producer 写入字段包括 `px/pc/amt/vol/iv/ia/ln/ts/ph/ls/mx/mn/spd1m/amt2m/amt5m/vec3m/vec5m` 以及竞价字段 `a20/a24/a25/am/br/ar` 和 `mk`；只有当前 Wheel 使用的核心字段才冻结到 canonical model。

## 2. Data Contracts

- [VERIFIED] 当前行情、盘口、累计成交和 Q2 freshness 属于实时状态，不应通过查询型数据函数读取。
- [VERIFIED] 历史日线、昨日数据、主题成分、历史 Tick 和参考价格属于查询型数据。
- [VERIFIED] Missing != Zero。
- [VERIFIED] fallback source != fallback semantic。
- [VERIFIED] 已验证连接方式优先复用；当前 engine_core 只抽取薄边界，不重新建设 Redis/TD/网络访问层级。
- [VERIFIED] `TemporalDataGuard` 只使用 `available_at_ms` 判断 knowledge cutoff；`available_at_ms` 未知或晚于 cutoff 的 READY/PARTIAL 数据一律 UNAVAILABLE。`observed_at_ms` 仅用于审计、provenance 和 submission identity，不能替代 available_at。
- [VERIFIED] `DataResult.content_hash` 在语义 payload deep-freeze 后派生，调用方不能手填；`FrozenDataBundle` 同时提供排除提交上下文的 semantic `content_hash` 和包含 evaluation/cutoff/status/observed/available 的 `submission_hash`。
- [VERIFIED] 正式 hash 合同固定为 `SemanticHashV1`、`EvidenceHashV1`、`SubmissionHashV1`；Probe trace 和验证证据必须记录合同版本。
- [VERIFIED] `TradingCalendarSnapshotV1` 是当前日期 authority：BaoStock 只读探查生成离线快照，运行时不联网；declared coverage 与 source guard coverage 分离，快照 semantic hash 与 evidence hash 分离。当前真实 BaoStock fixture 在 2026-12-31 右边界没有 2027 successor guard，`next_trade_day` 对该边界 fail closed，不能用合成日期补齐。
- [VERIFIED] `PreviousDayStatsFunction` 只接受交易日请求，并由版本化 CalendarSnapshot 唯一派生 `previous_trade_date`；调用方不能通过 DataContext 注入日期。
- [VERIFIED] `ReadyDataStore` 是最小进程内 readiness 字典，按 function/date/symbol scope/content hash 保存 READY `DataResult`，读取时重新经过 `TemporalDataGuard`；它不是 DataCatalog、Registry 或持久化 checkpoint。

## 3. Runtime Timeline

- [VERIFIED] 当前已验证的输入是 Q2 projection snapshot fixture/live adapter 和 fixture fact slice。
- [VERIFIED] 最小 Q2Frame + VirtualClock replay 已实现：严格校验 `Q2FrameV1` 版本、连续 `seq_no` 和不倒退 `logical_ts_ms`，逐帧生成已有 `MARKET_UPDATE`，不接管 Rabbit 或外部写入。
- [VERIFIED] 所有窗口使用半开区间 [start_inclusive, end_exclusive)。
- [VERIFIED] Timer 与行情输入分离。
- [VERIFIED] 重启跨过节点时使用 RECOVERY_CATCHUP，不伪装为正常准时执行。
- [VERIFIED] `DeterministicEngine` 在入队时冻结 signal payload；相同 `signal_id` 的相同内容在有界内存幂等窗口内幂等，冲突内容拒绝。MARKET_UPDATE/PULSE/TIMER 不得倒退 market frontier，旧 evaluation 的 `DATA_READY` 仍可使用原冻结 Snapshot 完成评估；持久化幂等留待 journal/checkpoint 阶段。
- [VERIFIED] `RECOVERY_CATCHUP` 关闭窗口时保留 `origin=RECOVERY_CATCHUP`，窗口 `finality` 仍为 FINAL。
- [VERIFIED] Engine Integration correctness gates passed for the tested scope: same-time ordering, DATA_READY ownership/isolation, PARTIAL propagation, old-evaluation isolation, bounded retained output history, duplicate/conflicting signal handling and same-time causal generation ordering. Evaluation registration is now separately bounded and fail-closed per session; cross-process durable identity remains deferred.
- [VERIFIED] 一个 Engine session 内 evaluation_id 只能注册一次；终态不会重新进入 pending。tombstone 仅保留有界近期分类，驱逐后仍 fail-closed 为 UNKNOWN。
- [HISTORICAL] 提交 `4e16150` 曾移除旧的 `evaluation_registration_limit=4096` 人工硬失败；该行为已由 `3d870ee` 修正为默认 65536 的有界、达到上限即 fail-closed 合同。历史 4097 次长会话测试不再代表当前无界语义；跨 session/跨重启持久化与轮换仍未实现。
- [VERIFIED] Foundation 600519 Segment A/B/Comparison hashes remain identical when composed through Engine; current Engine is only an orchestration boundary and does not alter fact-wheel semantics.
- [VERIFIED] TD Event-Time Replay 最小适配器按事件时间、代码和保留原始字段内容 hash 稳定排序，按三秒半开 `EVENT_SLICE` 保留逐条 tick，并逐事件提交同一个 Engine；缺少 symbol 时保持 PARTIAL。

## 4. System Capabilities & Fact Authority

- [VERIFIED] 旧 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 链路属于外部成熟生产链，engine_core 首期不接管。
- [VERIFIED] Redis Q2 是运行时投影，不是历史事实权威。
- [UNKNOWN] TD 生产版本、重复写语义和历史输入排序能力待 Gate K 验证。
- [VERIFIED] cobra-ion 上 TD `market_data1.stock_tick_v2` 可由既有 taos 客户端只读访问；2026-09-03 09:20-09:24 查询返回 82183 行，查询排序为事件时间/代码顺序，不代表 Rabbit arrival order。
- [VERIFIED] cobra-ion 上 `auction_snapshot_v2` 存在 2026-09-03 的 09:20:03 与 09:24:10 真实快照；600519 的匹配金额、resting bid/ask 与价格已固化为只读证据，可用于第一条 Segment A/B Golden fixture。
- [VERIFIED] 2026-09-10 在 cobra-ion 通过既有 `taos` 客户端对 2026-09-09/600519 的 0920/0924/0925 `auction_snapshot_v2` 执行只读事实旁路；业务锚点与 source record time 分离，Segment/Comparison 输出保持 `FACT_ONLY/PARTIAL`，不产生策略结论或外部副作用。
- [VERIFIED] 2026-09-10 对 `000001/000002/600519` 的 Redis auction projection 与 TD `auction_snapshot_v2` 进行了字段受限交叉验真：可比较的 amount/match 与 bid/rest_bid 全部 MATCH，快照 `meta.ts` 与 TD `ts` 精确对齐；Redis ask 字段当前不可比时保持 `NOT_COMPARABLE`。
- [VERIFIED] 2026-09-10 真实 Redis `market:auction:{date}:{0920,0924,0925}` Top-200 投影可直接经 source-specific normalization 进入 `engine_core` Segment/Comparison；600519 的价格、竞价金额和买方金额变化稳定复现，Redis 缺失的 ask 字段保持 `None/UNAVAILABLE`，结果固定为 `FACT_ONLY/PARTIAL`。
- [VERIFIED] 2026-09-13 以 Cobra 生产审计目录中真实捕获的 `2026-09-07` Redis 0920/0925 文件重跑旧窄读取器：两标签各 200 行、600519/300308/688825 六行选中样本、重复 `(tag,symbol)=0`、Guard 写入 `0`。该证据证明非空 captured projection 可安全只读读取；由于没有 0924 且每标签仅 Top-200，不宣称相邻段、全市场快照或 live Redis 保留能力。详见 `docs/evidence/engine_next_auction_loader_real_capture_20260907.md`。
- [UNKNOWN] cobra-ion `daily_kline.volume` 的零值语义；Probe 样本为 0，不能直接当作 verified zero。
- [VERIFIED] CurrentMarketState 只保存当前可观测数据、轻量 projection 和窗口原始累计状态。
- [VERIFIED] 2026-09-04 在 cobra-ion 只读 Redis Q2 子集（64 symbols）已通过当前内存 Engine 完成 `MARKET_UPDATE -> TIMER -> EngineSnapshot -> ProbeStrategy`；该次显式 freshness policy 下 1 条记录 stale，因此 projection 为 PARTIAL，不代表生产默认 freshness。
- [VERIFIED] 首个 SegmentFrame 事实切片仅支持 SYMBOL 范围；其他范围显式返回 UNAVAILABLE，不伪装成聚合结果。
- [VERIFIED] SegmentFrame 的 Price/Volume/OrderBook/Breadth/Theme 各自维护状态；未知累计量语义不计算 delta，返回 UNAVAILABLE。
- [VERIFIED] directional_pressure 仅是 Q2 盘口字段差值代理，不得命名为真实资金净流入。
- [VERIFIED] 基础轮子可脱离 Engine 单独运行：Contract、Q2、Window、TemporalDataGuard、SegmentFrame、相邻段比较、昨日数据行归一化和真实 600519 竞价段 fixture 均有单项测试。
- [VERIFIED] Gate B 已澄清 09:25 的两层边界：cobra-ion t1-v2 在 `09:25:06` 通过 settling barrier 形成源 A25；同一发布包 Python runtime 在 `09:25:10` 前保持等待、之后才发出正式消费事件。二者分别是 source snapshot gate 与 legacy consumer gate；engine_core 不在本阶段自行合并。
- [VERIFIED] 当前真实 600519 竞价事实只纳入 `price_milli`、`auction_amount_yuan`、`auction_bid_amount_yuan`、`auction_ask_amount_yuan`；Segment A `[09:15,09:20)` 为 PARTIAL，Segment B `[09:20,09:24)` 为 READY，主题/市场宽度不进入该最小案例。
- [VERIFIED] 竞价事实比较的最小可迁移对象是 `P/M/RB/RA` 的段间变化；它属于 Fact，不等同于 `turn_strong`、`BUY` 或其他策略结论。
- [VERIFIED] `AuctionFactShadow` 只对同一标的相邻 Segment 输出 P/M/RB/RA/pressure 端点变化和事实比较标签，固定为 `FACT_ONLY/OBSERVE`；它不迁移旧系统的买盘阈值、转强/转弱、撤单或波动策略。
- [VERIFIED] 2026-09-13 Gate B 在 cobra-ion 对真实 TD `600519` 的 0920/0924/0925 三锚点执行两次只读 shadow：Segment、Comparison、semantic/evidence hash 完全一致，结果保持 `PARTIAL/FACT_ONLY/OBSERVE`；运行只发出 TD `SELECT`，不涉及 Redis/TD 写入、Rabbit、repair、通知或策略 effect。详见 `docs/evidence/gate_b_fact_shadow_20260913.md`。
- [VERIFIED] 2026-09-13 23:16 CST 使用当前仓库归档 `20fbe959d041d7028d4f6f28847fc9109fb79138` 在 cobra-ion 生产共享 Python 3.12.3 上再次执行真实 TD `auction_snapshot_v2` 只读 shadow 两次；两次 semantic/evidence hash 一致，0920 价格缺失保持 `PARTIAL/FACT_ONLY`，没有把投影行误报成完整策略输入。最新 artifact SHA 为 `e7a06126d3d9d89e5aeb66f6400771363d5a21297341ac2c111c55126d31c7d0` 与 `a9977c6fd994474bef5356bc4f0b7d7ddf01b2a35c0a152090e3d2f9cdc2ba41`，详见 `docs/evidence/real_td_auction_shadow_20260913_2316.md`。
- [VERIFIED] 2026-09-13 23:29 CST 当前提交 `12045dd` 的完整离线套件在本地与 cobra-ion 生产共享 Python 3.12.3 临时归档中均为 `306 passed`，两端 `compileall` 通过且未触碰生产服务或数据；详见 `docs/evidence/cobra_exact_verification_20260913_2329.md`。
- [OBSERVED] 2026-09-13 对 Cobra 当前 `engine_next` release `e272842c8f490f55a1b017badb71e71904ce008e` 的实际竞价 consumer 调用和 `auction_bucket_concentrated/breadth/fade` 分支阈值完成只读源审计；这只证明调用路径与源码阈值存在，不证明实盘可达、单位/生命周期或 legacy consumer oracle，继续禁止迁移正式策略。详见 `docs/evidence/legacy_auction_consumer_active_path_20260913.md`。
- [VERIFIED] 2026-09-13 以当前 core commit `c9912a5` 重跑 Cobra 真实 TD 600519 竞价影子：0920→0924 为 PARTIAL（0920 price 缺失），0924→0925 为 READY，shadow 仍为 `PARTIAL/FACT_ONLY/OBSERVE`；price/amount/rest bid/rest ask/pressure 的端点变化和 source record time 均保留，artifact SHA-256 `99646FD66E61A4AE13CA72302967D18627B8E5F246D23BBA4B0D71BA993EE350`，详见 `docs/evidence/real_td_auction_shadow_20260910_current_commit.md`。
- [VERIFIED] 600519 的 09:20→09:24 相邻竞价事实已通过独立 source-formula 差异测试：`P_delta=-2,060` milli、`M_delta=4,407,516` yuan、压力由 `-129,960` 变为 `648,770`（压力差 `778,730`）；该测试只验证事实轮子，不验证策略阈值。
- [UNKNOWN] 旧系统中分散出现的 `bid_amount > ask_amount * 1.5`、撤单和波动阈值尚未完成当前生产路径、单位、consumer 和状态生命周期的闭环验证，不得直接迁移为正式策略。
- [OBSERVED] 2026-09-10 生产 `t1-v2-live.service` 实际入口为 `C/t1_v2/main.cpp → RuntimeLoop`；旧 `C/analysis.cpp`/`C/t1.cpp` 中的 `bid_amount > ask_amount * 1.5` 不在已确认的活动 t1_v2 入口内，不能充当当前 live strategy oracle。
- [VERIFIED] 当前基础轮子可在 cobra-ion 的 Python 3.12.3 server venv 临时验证副本中运行；这不是生产部署。
- [VERIFIED] `normalize_previous_day_stats_rows` 是旧日线访问结果的薄纯边界：严格校验六位代码、保留显式零值、拒绝缺失核心字段/重复代码，并输出稳定排序的昨日统计映射；空结果经 Provider 包装后为 MISSING。
- [VERIFIED] Calendar snapshot 的 `completion_cutoff_time` 是调用侧数据完成策略，不是日历 source fact；naive datetime、非交易日请求和超出覆盖范围均 fail closed。
- [VERIFIED] `MinuteWindowTracker` 是独立的分钟窗口轮子：按显式 source epoch 时间和 Asia/Shanghai 分钟桶保存有界累计价格/金额点，1 分钟价格变化要求相邻分钟，2 分钟金额沿旧实现使用前两分钟内最早可用参考点；缺参考或累计值回退不伪造增量，不使用系统当前时间。
- [VERIFIED] `SessionPlanV1` 将“是否交易日”和“交易日内属于哪个阶段”分开：`TradingCalendarSnapshot` 先确认指定日期可交易，计划再用显式 Asia/Shanghai 时区和全天连续半开区间分类；周末/节假日不再因墙钟时刻被误判为 AUCTION/INTRADAY，15:00 明确属于 POSTMARKET。
- [VERIFIED] Engine 可选择 `SessionPlanV1` 作为唯一 phase authority：MARKET_UPDATE 按自身 logical time 记录观察阶段，TIMER/PULSE/RECOVERY 按触发 logical time 冻结阶段；09:24 行情后即使没有新行情，09:30 Timer 仍产生 INTRADAY Snapshot，且不会篡改上一行情观察的 AUCTION phase。配置 SessionPlan 时禁止同时注入非 UNKNOWN 静态 phase，跨计划交易日的信号 fail closed。
- [VERIFIED] `SessionTimerV1` 是无时钟、无持久化的到期计算轮子：显式区分 business scheduled time 与 actual fired time，通过调用方提供的 fired identity 保证一次性，并用 NORMAL/RECOVERY_CATCHUP 标记正常跨越与重启补发；t1-v2 的 09:20:03/09:24:10/09:25:06 source freeze gate 不属于该 Timer 合同。
- [VERIFIED] `EvaluationPlanV1` 只以普通不可变数据描述 trigger 对应的 DataFunction、FactFunction 和 Strategy 名称及固定顺序；它不执行 callable、不包含条件跳转、DAG、retry、fallback 或并发编排。
- [VERIFIED] Engine 对 `EvaluationPlanV1` 的首个接入边界只兑现当前真实能力：plan 以 exact trigger 提供固定 DataRequirement 顺序，并且必须声明 Engine 已绑定的唯一 Strategy；signal payload 不得覆盖 requirements。当前 Engine 尚无 FactFunction executor，因此非空 `fact_functions` 会在构造时 fail closed，绝不静默忽略；plan/node hash 已绑定 evaluation identity。
- [VERIFIED] Q2 rolling fields `spd1m/amt2m/amt5m/vec3m/vec5m` 已进入显式 canonical contract：速度/向量为整数 basis points，金额窗口为整数 yuan；当前生产 `C/t1_v2` 负责计算，`engine_next` opening consumer 实际读取这些字段。它们是窗口事实，不是强弱或资金流结论。
- [VERIFIED] cobra-ion 上 `TDPreviousDayStatsProvider` 已通过既有 taos 只读路径取得 2026-09-03 的 3 行 `daily_kline`；因没有历史 `available_at` 证据，`PreviousDayStatsFunction` 按规则返回 UNAVAILABLE，而不是把查询时刻冒充可用时刻。
- [VERIFIED] 2026-09-10 09:08 在 cobra-ion 以生产 Python 3.12 `taos` 客户端通过薄 `TDPreviousDayStatsProvider` 读取 `market_data1.daily_kline` 的 2026-09-08 三行真实数据；请求交易日为 2026-09-09，上一交易日由日历唯一派生，三只标的全部返回，但因 `available_at` 未知按合同返回 `UNAVAILABLE`，无任何写入。
- [VERIFIED] TD 昨日数据没有历史 `available_at_ms` 证据时，无论首次查询时刻还是节点前预取，Runtime/Replay 均按 UNKNOWN availability 返回 UNAVAILABLE；只有具备 verified `available_at_ms <= knowledge_as_of_ms` 的结果才可进入 FrozenDataBundle。`observed_at_ms` 仅保留为审计和 submission identity。
- [VERIFIED] cobra-ion live Q2 + TD shadow path completed without writes: Q2 5217/5217 coverage with 10 stale symbols, TD previous-day result UNAVAILABLE due unknown availability, FrozenDataBundle completeness 0.0, SegmentFrame PARTIAL with price/pressure READY, and Probe trace preserved PARTIAL.
- [OBSERVED] 2026-09-09 对生产 Redis Q2 的多次只读探针均返回 5218/5218 coverage，但 newest source lag 从约 193 秒扩大到 471 秒，全部记录在显式 120s/300s freshness policy 下为 STALE；生产 engine_next 同时记录 `live_quote_ready=False`。coverage 不能替代 freshness。
- [VERIFIED] 2026-09-09 cobra-ion 真实连接探针确认 BaoStock、开盘啦三类接口、问财和 THS 均可连接；只有 BaoStock 日线同时闭合请求日期与响应日期。其他源当前只获得 connectivity/observed 证据，不能作为历史 replay `available_at` authority。
- [VERIFIED] 2026-09-13 用当前 core commit `648b07e` 在 Cobra 生产共享 Python 3.12 环境重跑真实参考源探针：6/6 connector connection PASS；仅 BaoStock `fetch_daily_kline(2026-09-10)` 同时满足请求/返回日期闭环，其余 Kaipan/THS/问财保持 OBSERVED，不能进入历史 runtime/replay。artifact SHA-256 `7DBD403888ADC9BEAA07E6E18B54A15661D0EC61E43BB4F532A5B02D84C0E591`，详见 `docs/evidence/real_reference_probe_20260910_current_commit.md`。
- [VERIFIED] 2026-09-09 生产只读 TD 查询取得 `auction_snapshot_v2` 的 0925 数据 5217 行；`daily_kline` schema 不包含发布时间或入库时间，因此它本身不能证明 historical `available_at`。
- [OBSERVED] 生产 t1-v2 与 Rabbit/Redis/TD TCP 均已连接且内置 `--self-test` 通过，但 `logging.file_path/enable_file_log` 当前未接入长期运行日志，正常运行时无法读取 batch/decode/ACK/commit counters。该可观测性缺口阻止定位 Q2 延迟首次发生的层级。
- [VERIFIED] 已在独立分支 `codex/fix-t1-live-observability` 提交 `9fd4a42b3f3944235da89e1ae2278ea93cff193c`，仅增加周期 batch/source/ACK/Redis/TD 计数、pipeline/commit/ACK 耗时和 wall lag；cobra-ion 完整生产依赖候选构建及 self-test 通过。该候选尚未替换生产二进制，不能作为生产根因证据。
- [VERIFIED] 2026-09-09 14:13 的 cross-source 只读对齐显示 TD 最新 Tick 为 14:01:15、Redis Q2 最新为 14:01:39，两者共同落后墙钟约 12 分钟；AMQP passive declare 同时显示单 consumer、队列 backlog `2457 -> 2469`。因此当前 freshness first divergence 在 Redis/TD writer 分叉之前，且 Rabbit 消费吞吐未追上生产。
- [OBSERVED] 2026-09-09 的 Redis 0920/0924/0925 仅为 `meta/summary/top_amount` 投影；Anchor 有 5183 个 symbol，但逐股只有 `change_pct/amount/bid_amount/tag/source`，不含完整 P/M/RB/RA。它不能冒充 TD `auction_snapshot_v2` 的全字段 authority，只能比较 shared fields。
- [OBSERVED] 生产 `engine_next.runtime.intraday_data_hub.load_auction_snapshots()` 只读取三个 Redis Top-200 投影并返回约 600 行；其 `IntradayFetchResult.redis_keys_written` 字段会列出读取过的 key，实际本次调用无写入。`recover_auction_anchor()` 是另一条可能执行 Redis 回写的路径，engine_core 旁路不得调用。
- [OBSERVED] 生产 engine_next commit 自带测试显式运行结果为 289 PASS / 32 FAIL；release 文件与 commit 在 CRLF 归一化后相同，失败源于 commit 内测试/实现漂移、漏打 fixture 和乱码断言，不能作为全绿发布门禁。
- [OBSERVED] 2026-09-10 在 cobra-ion 对生产 `engine_next` 的 `IntradayContextBuilder` 执行少量标的只读 guard 审计：snapshot/auction/session-fact 读取成功且本次无 Redis/TD/网络写入；但 builder 默认路径包含 hot-rank 缓存、session-fact 写入、SectorFlowTracker 写入、F10 fallback 和 auction anchor recovery 等潜在副作用，不能直接当作 engine_core 的只读 Provider。
- [VERIFIED] 2026-09-13 对当前 Cobra release `e272842c8f490f55a1b017badb71e71904ce008e` 完成 provider→writer→reader 只读审计：KaipanConnector 复用 `StockAnalyzer`/pykaipan，IntradayDataHub 负责 hot_plates/hot_rank/yest_limit_pool/limit_truth Redis 写入，app_main/prior_limit_cache_contract 负责读取与 payload-hash 校验；实测日期分区均为 hash 且有界扫描 HLEN 一致，但当前元数据仍缺 verified `schema_version`/`available_at_ms`/field_units，因此 core 对 hot plates/昨日涨停池返回 UNAVAILABLE 属于正确 fail-closed，M2 继续 BLOCKED，未改生产。
- [VERIFIED] 2026-09-13 对同一 release 的旧竞价/开盘调用链完成定向能力审计：`load_auction_snapshots()` 是 Redis 0920/0924/0925 TopN/summary 的只读投影读取；`recover_auction_anchor()` 可能按 Redis→TD→问财 fallback 并 `SET market:auction:anchor:{date}`，不得抽成 engine_core 只读 Provider；`execute_auction_finalize_0925()`/`execute_auction_followup_0926()` 会刷新多个缓存；`build_auction_plate_bucket_stats()` 与 `build_opening_validation_bundle()` 含策略阈值但缺活动 consumer oracle，继续保持 UNKNOWN。详见 `docs/evidence/legacy_capability_matrix_20260913.md`。
- [VERIFIED] 2026-09-13 M2 authority closure 进一步确认：hot-plates reader 仅依据缓存存在、row_count、日期和 `updated_at_ts` 判 ready；昨日涨停 reader 虽校验 payload hash，但 `cache_preserved` 只证明保留了旧 payload，不能证明历史知识可用时间。真实 `turnover` 为大额金额量级而 legacy schema 标成 percent，单位仍 UNKNOWN；在版本化 `available_at_ms/field_units` writer metadata 真正部署并完成 Cobra shadow 前，两个数据集均不得进入 core runtime。
- [CANDIDATE] 2026-09-13 已形成最小 `engine_next.hot_plates_meta.v1` writer-contract spec（SHA-256 `9d95a64c2de9ae86f9475f0f0dec2a80164b6adbf5429ad9716fd61f2b2ec5fd`）：仅补 `HotPlatesV1`、`available_at_ms`、显式 `field_units`、canonical `payload_sha256` 及既有日期/来源/数量元数据；不改查询、Redis key、算法或副作用。该 spec 只允许后续候选验证，生产实际生成 metadata 前 M2 仍 BLOCKED。
- [VERIFIED] 2026-09-13 对上述热板候选合同完成本地全量验证（271 passed、compileall、diff-check）；Cobra 当前 `e272842` 只读复核仍显示旧 metadata 缺 `schema_version/available_at_ms/field_units/payload_sha256`，证据 `docs/evidence/m2_hot_plates_candidate_verification_20260913.md`（SHA-256 `bd1b3e8b8bed248adfb60a5f2eadc2f79709df24bac87d575f5edfcb297b258a`）。候选未部署，M2 authority closure 继续 BLOCKED。
- [CANDIDATE] 2026-09-13 v2 热板 metadata 候选在全新 `e272842` 副本中通过 literal plan/apply/idempotent-check；候选提交 `53a45ebb170a33a57d4c46aa4a4d699a17e3fe16`、tree `11b7e2f6174b5c344850c9e04cf8a467ca500fa4`、recipe SHA-256 `be975b49e6fb447078c1ad687f4f93479aadf5f9f21863c68348a22aead92fd4`。它只在 `schema/units/row_count/actual payload hash/positive availability` 全部匹配时保留旧缓存 metadata；payload 或 units 不匹配则 availability unknown，不做 salvage。受控 Ubuntu/Podman 精确验证 PASS（manifest `972ced0e73a84241508b3562e24da5b438cbdeb53ffccbc3f693aa5816242808`），但独立审计再次被本机 PowerShell `8009001d` 阻塞；最新 doctor 检查显示 gateway liveness 正常、readiness HTTP 500，故候选 IN_DOUBT、生产 metadata 仍 BLOCKED；同日 cobra-ion 隔离目录对 Core 当前工作树运行 271 tests/compileall PASS，但不改变候选和生产状态；详见 `docs/evidence/m2_hot_plates_candidate_verification_v2_20260913.md`（SHA-256 `effd5eb4cc026e088ca79e2d8043a2677503cb1349830b2748f126fc66d127da`）。
- [OBSERVED] 同次审计通过显式注入 `now=2026-09-09 09:26` 复现旧 freshness 逻辑对未来 Q2 source timestamp（约 15:00:03）使用 `max(now - source_ts, 0)` 并报告 age=0/fresh；这不是现场 09:26 墙钟证据，而是可重复的时间安全测试。该旧读取路径时间安全结论为 FAIL；engine_core 继续以 `max_future_skew_ms` 失败关闭为目标，本分支不修改生产代码。

## 5. Replay Capabilities

- [VERIFIED] 当前只支持 Q2 projection fixture/live slice、Q2Frame 和 TD event-time replay；不支持 REPLAY_RECORDED，也不宣称 Rabbit arrival/batch 等价。
- [VERIFIED] Q2Frame replay 与同一规范化 Q2 fixture 进入同一 Engine 的 semantic snapshot/Probe 结果 EXACT_EQUIVALENCE 已通过；该结论仅覆盖 Q2Frame 的 logical timestamp/逐帧 projection，不代表 Rabbit arrival/batch 等价。
- [VERIFIED] TD Event-Time Replay 已通过本地与 cobra-ion 只读验证；当前能力是 `DETERMINISTIC_EVENT_TIME_ONLY`，不恢复 Rabbit arrival/batch，不做 watermark 或 late correction。
- [VERIFIED] TD Event-Time Replay 在同一 `event_time + symbol` 且缺少真实 source ordering key 时，使用原始字段内容 hash 作为 deterministic synthetic tie-break；该 hash 不代表生产真实先后。
- [VERIFIED] Replay signal construction 不推进共享 VirtualClock；按 logical time 分组后在 Engine 消费前推进一次，同刻 child signal 进入下一 causal generation，不能越过当前 generation 的 parent/sibling。
- [VERIFIED] 不同信息粒度使用 EXACT_EQUIVALENCE 或 SHARED_FACT_EQUIVALENCE。
- [VERIFIED] replay 默认 deny-all effect，并通过 available_at <= knowledge_as_of 防止未来数据穿越；未知 available_at 不能被 observed_at 推导。
- [VERIFIED] Real Data Probe 只作为字段/连接证据和 fixture capture；当前能查到历史数据不等于历史 `available_at` 已早于 replay 的 `knowledge_as_of`。

## 6. Known Pitfalls

### PITFALL: Q2 projection 被误称为全市场同刻快照

问题：
- Redis 读取的是各股票最近状态，读取时间和 source 更新时间可能不同。

正确做法：
- 记录 observation time、source update range、coverage、consistency_status 和 content hash。

### PITFALL: fallback 改变业务语义

问题：
- 缺昨日数据时错误使用前日数据并返回 READY。

正确做法：
- 只有业务定义等价的来源才能 fallback；实际日期不符返回 STALE。

### PITFALL: async completion order 影响业务顺序

问题：
- 多个 DataFunction 先返回哪个就先构造 Bundle。

正确做法：
- 按 evaluation_id 收集全部 required 结果，再按 DataRequirement 固定顺序构造 FrozenDataBundle。

### PITFALL: EVENT_SLICE 预先丢失 Tick 信息

问题：
- 把三秒 slice 提前压成 OHLC 或 Feature，无法验证五档路径和 first-touch。

正确做法：
- EVENT_SLICE 只是 transport/scheduling batch，保留逐条 Tick 和稳定顺序。

### PITFALL: 端点快照冒充完整时间段路径

问题：
- 只有时间段起止快照时，无法证明段内最高/最低或 first-touch 路径。

正确做法：
- SegmentFrame 仅输出起止可观察事实；high/low、段内路径等字段保持 None，等待逐条事件输入。

Evidence：
- tests/test_facts.py、examples/run_fact_vertical_slice.py
- docs/evidence/ 后续保存 Gate K、Vertical Slice 和 Gate B 原始报告。

### PITFALL: 把已能查询的数据当作历史时点可知

问题：
- 今天的网络或 TD 查询可能返回历史最终值，但不证明交易时点已经可见。

正确做法：
- Runtime/Replay 统一经过 `TemporalDataGuard`；若没有历史 `available_at` 证据，数据一律不能作为该 cutoff 的 runtime input，只能用于 oracle、Contract 验证或 fixture capture。`observed_at` 仅记录实际观察时间，不得替代或推导 `available_at`。

不要：
- 从当前网络查询结果反灌历史 replay runtime。

Evidence：
- `docs/evidence/real_data_probe/20260904T082940+0800/provider_summary.md`

### PITFALL: 节点时刻临时查询参考数据

问题：
- 节点触发时才查询昨日/参考数据，查询观察时间可能已经晚于节点的 `knowledge_as_of`，即使数据业务日期属于昨日也不能安全进入该评估。

正确做法：
- 在节点前通过同一 `DataFunction` 只读预取，经过 `TemporalDataGuard` 后放入最小 `ReadyDataStore`；节点只读取已冻结结果。

不要：
- 为了让 Replay READY 而填写猜测的 `available_at`，或把当天查询时间反推成历史首次发布时间。

Evidence：
- `docs/evidence/reference_data_readiness_20260904.md`

### PITFALL: 重新发明已验证的连接方式

问题：
- 新 Provider 若绕过旧系统的 host、key、session、查询和错误处理，会得到与生产链不同的数据能力。

正确做法：
- 先做 Legacy Connectivity Inventory，只对当前 Wheel 需要的源采用 KEEP/EXTRACT/WRAP；Provider 保持薄边界。

不要：
- 第一阶段建设通用 `RedisAccess/TDAccess/NetworkAccess` 层级。

Evidence：
- `docs/evidence/foundation_audit.md`

### PITFALL: 合成 Tie-break 被误称为生产到达顺序

问题：
- TD Event-Time Replay 可能遇到同一 `event_time`、同一 symbol 且没有 source sequence 的多行事件。

原因：
- 为了让回放可重复，当前实现使用保留原始字段内容 hash 做最后排序键；TD 未保存 RabbitMQ arrival/batch 顺序。

正确做法：
- 将该 hash 明确标为 deterministic synthetic ordering；只用于稳定回放，不用于解释生产先后。
- 未来只有找到真实 row/source sequence 才能替换排序键。

不要：
- 把 TD 查询顺序或 raw hash tie-break 当作生产 arrival order。

Evidence：
- `docs/evidence/td_event_time_replay_20260904.md`
- `docs/evidence/gate_b_auction_open_audit_20260904.md`

### PITFALL: 旧版盘口金额公式与当前发布包不一致

问题：
- 本地旧版 `C/t1_v2` 曾按二档价格×二档手数计算 `br/ar`，cobra-ion 当前发布包已按一级价格×二档手数计算。

正确做法：
- 以当前运行环境的发布源和真实原始行复算 Golden fixture；保留公式修正证据，不把旧派生值直接当作当前契约。

不要：
- 只复用旧 fixture 的派生字段，跳过当前 producer/consumer parity。

Evidence：
- `docs/evidence/gate_b_formula_correction_600519_20260905.md`
- [VERIFIED] 2026-09-16 Core commit `b0f8f4b` added a thin read-only provider for the existing `cache:kline_ready:{date}` Redis view and made the real readiness runner select it explicitly only after a successful empty TD read. Local and cobra-ion exact-archive suites both pass `436`. A real bounded run found all three requested Redis daily-kline rows, but no kline availability metadata exists, so the result truthfully changed from `MISSING` to `UNAVAILABLE` rather than fabricating `available_at`. Production services remained active. See `docs/evidence/real_redis_previous_day_stats_20260916_b0f8f4b.md`.
- [VERIFIED] 2026-09-17 premarket exact Core `8b3a207` ran on cobra-ion with 438 tests passing and no production side effects. The real readiness probe derived 2026-09-16 as the prior trade date, found 89 Redis yesterday-limit rows and real Redis kline rows after an empty TD read, but correctly kept both reference results `UNAVAILABLE` because production availability metadata is absent; Q2 was not yet published and hot plates were missing. See `docs/evidence/real_auction_reference_readiness_20260917_8b3a207.md`.
# [VERIFIED] 2026-09-18 minimal `SessionRuntimeCoordinator` M1 boundary: local and cobra-ion isolated validation both pass 460 tests and compileall. The coordinator is in-memory only, composes existing calendar/session/readiness/timer wheels, and does not perform I/O, persistence, Rabbit consumption/ACK, Redis/TD writes, or effects. A real 12:50 CST Redis Q2 probe observed 5224/5224 coverage but all symbols stale under the explicit 10-second policy; readiness remained PARTIAL and repeated probe/snapshot hashes were deterministic. This is runtime-coordinator verification and stale-data shadow evidence, not normal opening acceptance or engine-next replacement. See `docs/evidence/m1_session_runtime_verification_20260918.md`.
# [VERIFIED] 2026-09-18 the bounded read-only Morning Shadow now uses `SessionRuntimeCoordinator` for timer identity and acknowledges each firing only after node evidence is written. Node-level gates remain explicit: `AUCTION_0926` can use the independent TD fact path without Q2, while `OPENING_0932` still fails closed when its Q2 read is unavailable, future, invalid, or otherwise blocked. Pending/deferred timer bookkeeping prevents a late-start 09:33 poll from losing a deferred 09:32 timer after acknowledging 09:26. Local and cobra-ion isolated suites both pass 464 tests with compileall; no production service, Rabbit consumer/ACK, Redis/TD writer, or effect changed. See `docs/evidence/m1_session_runtime_shadow_integration_20260918.md`.

# [VERIFIED] 2026-09-18 `PreviousDayLimitFeedbackFactV1` adds the smallest current-session feedback slice beside the existing previous-limit structure wheel. It joins a guarded previous-session limit-up pool with normalized `0925` rows and derives only objective return counts/ratio/median, board-height structure, source-time range, and per-symbol lineage. It requires explicit `change_pct` percentage-point semantics and keeps `source_record_time_ms` separate from the `0925` business anchor; missing return or source-time evidence is not converted to zero and cannot claim `READY`. The fact is now an optional build-only section of `AuctionFactReportArtifact`, carrying semantic/evidence lineage without delivery effects. Local full suite is 498 passed with compileall; the real 47-row Redis capture remains runtime `UNAVAILABLE` because historical `available_at` is unknown. Plate aggregation, formal return authority, strategy thresholds, and production writers remain outside this slice. See `docs/evidence/previous_day_limit_feedback_20260918.md`.

# [VERIFIED] 2026-09-18 bounded real feedback probe: Cobra-ion read 47 physical rows from the real Redis previous-limit hash and 3 bounded `0925` rows from the existing TD `auction_snapshot_v2` SELECT path for `2026-09-18` / `2026-09-17` (`000001,000002,600519`). The new fact returned `UNAVAILABLE` with no row leakage because Redis history has no verified `available_at`; the probe performed no Redis/TD writes, repair, Rabbit action or effect. Artifact SHA-256 `f6c67bb6bddd672fc112d039559bb49fbe66a5660ec1d47200a9236b1d65648`. Local and Cobra isolated suites are now 500 passed with compileall. See `docs/evidence/previous_day_limit_feedback_20260918.md`.
