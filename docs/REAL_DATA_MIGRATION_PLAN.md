# engine_core 真实数据与生命周期迁移计划

### 2026-09-14 报告/通知边界补充

- 只读审计确认旧 `engine_next` 的 `build_auction_email_report()` 是报告表示构建边界：它消费已形成的 shadow/evidence/context，生成 HTML/文本及哈希；SMTP、Webhook 和通知去重由 `RuntimeNotificationService` 持有。Core 后续只允许提供冻结事实上的 build-only 报告产物，不接管通知副作用、隐式补数或旧 runtime controller。详见 `docs/audit/engine_next_report_boundary_20260914.md`。

## 基线与执行状态

### 2026-09-14 日历证据格式兼容修复

- 发现明日只读 Morning Shadow 使用的 `cc-m0-calendar-20260911-v3.json` 是 `RealCalendarProbeV1` 原始证据格式，旧 loader 会因缺少 `calendar_id/timezone/source_guard_*` 直接 fail-closed；这不是数据损坏。提交 `6baba2d` 使只读 loader 同时接受该格式和 `TradingCalendarSnapshotV1` fixture，按 `query_start/query_end` 重建不可变快照并校验 `calendar_semantic_hash`，新增两项回归测试。Cobra-ion Python 3.12.3 对精确归档为 `395 passed`、`compileall PASS`，无生产写入。已用修复副本重新安排明日 09:15–09:33 Shadow；详见 `docs/evidence/calendar_probe_loader_fix_20260914.md`。

### 2026-09-14 六层生产链捕获审计（最新）

- 18:30 在 Cobra-ion 使用现有只读 runner 重做真实 Provider 复验：Redis Q2 5220/5220、coverage=1.0 但全量 `STALE`；TD 上一交易日真实返回 3 行但因 `available_at` 未知按合同为 `UNAVAILABLE`；Redis/TD 竞价对照 `mismatch=0`，但 5 条 `PARTIAL_COMPARABLE`、4 条 Top-200 外 `NOT_COMPARABLE`。未写 Redis/TD、未碰 Rabbit/通知。详见 `docs/evidence/real_provider_probe_20260914_1830.md`。
- 同一只读日志切片还显示累计 `source_reject`（09:25 为 3,674,985，15:23 为 5,494,355）以及少量 `last_reject`，但当前无法把计数映射到具体 decode/字段分支；与 wall lag 的因果关系保持 UNKNOWN，已纳入多交易日输入完整性门槛，未修改 producer。详见 `docs/evidence/runtime_input_reject_observation_20260914.md`。
- 测试范围审计已完成：当前 46 个测试模块、319 个静态测试函数，pytest 参数化后 393 个收集用例；本地/Cobra 均通过。但其中真实源验证由显式 Cobra 只读 probe 独立完成，393 passed 不代表 393 次真实连接；真实 in-session Shadow、批边界、writer 同源和 Core replacement 仍未闭合。详见 `docs/evidence/test_scope_audit_20260914.md`。
- 当前提交 `cf90956` 已完成固定归档的跨环境复验：本地与 Cobra-ion Python 3.12.3 均为 `393 passed`、`compileall` PASS，归档 SHA-256 一致；本次只确认代码/证据提交可重复执行，不改变生产接受结论。详见 `docs/evidence/verification_cf90956_20260914.md`。
- t1-v2 生产日志的只读切片显示 09:25 `wall_lag_ms=2418`，09:30 已为 `12222`，15:23 达到 `1428792`（约 23.8 分钟）；采样行 `ack_fail=0` 且服务仍 active，但存在持续积压/性能风险。该事实加入 Core 替代前的多交易日容量门槛，未在盘中修改 producer。详见 `docs/evidence/runtime_lag_observation_20260914.md`。
- 读取现有生产日志补齐了一段 09:24:50–09:26:30 runtime 证据：t1-v2 在 09:24:51/09:25:02 分别报告 `batches=110927/110953`、`ack=110927/110953`、`ack_fail=0`、source-time 与 wall lag；engine-next 在 09:25:10 执行 `auction_finalize_0925`，09:25:48–09:25:49 完成 5219 条 runtime context，09:26:27 执行 `auction_followup_0926`。这仍不能证明具体 final Tick batch membership、AuctionState freeze 或 Redis/TD writer 同源一致。详见 `docs/evidence/runtime_log_slice_20260914_0925.md`。
- 18:06 在 Cobra-ion 复用旧 release 连接器完成有界真实参考源探针：Baostock `fetch_daily_kline(2026-09-11)` 请求/返回日期均闭合；Kaipanla 热板/昨日涨停池/原因、问财涨停池、同花顺热度均返回真实样本但缺结构化历史日期或 `available_at` 证据，继续保持 `OBSERVED`，不得进入历史 Replay runtime。6/6 connector calls PASS；响应中的旧中文编码异常原样保留为证据。详见 `docs/evidence/real_reference_probe_20260914_1806.md`。
- 盘后补做了两个独立的 `engine-next` 真实 Redis 只读探针：当前 release 的 `load_auction_snapshots()` 在观察时点返回 0920/0924/0925 各 200 条 Top-200 projection，旧 context builder 对 3 个 bounded symbol 也成功读取，两个 Guard 均 `guard_writes=[]`。这只关闭 loader/context read-only 证据，不把不同 projection 宣称为 parity；Rabbit batch、AuctionState/freeze 和同源 as-of 仍为 `UNKNOWN/UNPROVEN`。详见 `docs/evidence/engine_next_loader_context_probe_20260914_1752.md`。
- 使用当前可执行提交 `cb5d6fd` 在 Cobra-ion Python 3.12.3 对真实 `2026-09-14` capture、真实 TD `auction_snapshot_v2` 源行和真实 TD Tick 样本执行 `run_production_chain_shadow.py`。产物包含 `production_chain_matrix.csv`、tick morphology 文件和 `audit_summary.json`；Q2 Core shadow 重复 hash 一致，auction source-row fact 为 `OBSERVED/FACT_ONLY/PARTIAL`，未接受预计算结果自证。
- 结果严格保持：`SOURCE_INGESTION=UNKNOWN`、`AUCTION_STATE=OBSERVED`、`STORAGE_PROJECTION=WARN`、`ENGINE_NEXT_CONSUMPTION=UNKNOWN`、`ENGINE_CORE_SHADOW=PARTIAL`、`JOINT=WARN`。`auction_0924` 在原 capture 时为空，未用后续 Redis/TD 观察回填；Gateway/Rabbit batch membership、内部 AuctionState/freeze 和 engine-next loader trace 仍未观测。
- 真实 Q2 `q2_093210.jsonl` 为 5220/5220、coverage=1.0，但 `STALE/BEST_EFFORT_STALE`，source-time range 保留；TD Tick 11 行同毫秒顺序保持 `UNKNOWN`，没有把导出顺序/hash 当作生产因果顺序。远端产物和 SHA-256 详见 `docs/evidence/production_chain_shadow_20260914_full.md`。
- 审计工具随后修正了独立 TD source-row fact 的矩阵归属：缺失的 `auction_0924` 与 aggregate-only `auction_anchor` 不再被标成 `engine_core=OBSERVED`，改为 `UNPROVEN`；独立 `auction_fact_shadow` 仍保留 `OBSERVED`。本地/Cobra 3.12.3 均为 `392 passed`、`compileall` PASS，最新矩阵 SHA-256 为 `f56144e42d36a3c6ba0457c05048a3ea8433038a710c6a412ade6cc775c18c25`。
- 该次是 captured-file 只读审计，不是生产服务连接验证，也不宣称 Core 已替代 `engine-next`；下一有效证据仍需正常盘中 09:15 前启动的 in-session run 与可安全取得的 engine-next loader trace。
- 审计工具收口提交 `7f24d33` 增加非空输出目录 write-once 保护，重跑不会覆盖既有证据；本地与 Cobra-ion Python 3.12.3 均为 `393 passed`、`compileall` PASS。该修正只影响证据写入边界，不改变生产链计算结果。

### 2026-09-14 Live Morning Shadow V1 最新提交复验

- `run_live_morning_shadow.py` 的最新修正提交为 `472bf7d`：统一 Q2 trace 中的 `DataStatus` 枚举输出为业务值，并补充节点 `input_sha256`/Q2 source-time metadata。Local/Cobra-ion Python 3.12.3 同一归档均为 `391 passed`、`compileall` PASS，归档 SHA-256 `0cb91fd65fab3cdc251558c74f284e803a265c7691f0c834ae42487915faecf7`。
- 2026-09-14 17:28 在 Cobra-ion 使用真实 Redis Q2、TD `auction_snapshot_v2` 和 GuardRedis 旧 auction loader 做晚启动只读运行；`AUCTION_0926`、`OPENING_0932` 均生成，origin=`RECOVERY_CATCHUP`，节点保留实际 17:28 observation time，未伪造 09:26/09:32 输入。manifest safety 的新增 consumer、ACK/publish、Redis/TD write、notification/effect、production restart 均为 0。该证据只证明晚启动组合可运行，不是盘中时点证明；下一交易日须在 09:15 前启动捕获真实节点。详见 `docs/evidence/live_morning_shadow_20260914_1728.md`。

### 2026-09-14 Live Morning Shadow V1（晚启动只读验证）

- 新增 `examples/run_live_morning_shadow.py`：有界只读运行壳，复用现有 `SessionPlanV1`/`SessionTimerV1`、Redis Q2 adapter、TD `auction_snapshot_v2` 查询、旧 Redis auction loader 的 GuardRedis 入口和既有 `dispatch_morning_fact_nodes`；不新增 scheduler、Provider、Rabbit consumer、ACK、writer、通知或 effect。每个节点在实际消费时刻读取，保留业务锚点与 source observation time，输出目录 write-once。
- 本地与 Cobra-ion Python 3.12.3 同归档均为 `390 passed`、`compileall` PASS；代码提交为 `44d6a46`，归档 SHA-256 `5a62e1de868b17b1f72b5fd5d526d9bdb16d4209957df23b3528b8ea6f7f60cc`。
- 2026-09-14 17:17 在 Cobra-ion 按晚启动 `RECOVERY_CATCHUP` 路径真实运行：startup、`AUCTION_0926`、`OPENING_0932` 三节点均生成；TD SELECT、GuardRedis loader、Redis Q2 读取只读完成，manifest safety 中 Redis/TD write、Rabbit ACK/publish、notification/effect、production restart 均为 `0`。节点观察时间原样为 `17:17:42+08:00`，业务锚点仍分别为 09:26/09:32；该结果证明晚启动的有界只读组合可运行，不等于盘中 09:26/09:32 时点证据，也不等于 Core 已替代 `engine-next`。证据保存在 `tmp/live-morning-shadow-20260914-1717/`，远端目录为 `/home/exedev/validation/live-morning-shadow-20260914-1717`。
- 下一交易日只需使用相同命令在 09:15 启动，等待真实 09:26/09:32 节点；不在盘后用历史最新数据冒充当刻输入。该 runner 仍不触发正式报告/邮件，`engine-next` 与 `t1-v2-live` 继续作为生产 owner。

### 2026-09-14 StartupReadinessV1 read-only closure

- 新增最小 `StartupReadinessV1` 纯轮子与 `run_startup_readiness_probe.py`：显式校验交易日/SessionPlan 身份，复用 Q2 状态和 source-time range，重新执行参考数据 `TemporalDataGuard`，并委托 `SessionTimerV1` 计算到期节点；不创建 provider、不预取、不写 Redis/TD、不接 Rabbit、不发 effect。提交 `4e7b2f1`。
- 本地与 Cobra-ion Python 3.12.3 同一归档均为 `386 passed`、`compileall` PASS，归档 SHA-256 为 `ab6ab6ee2a5cc0775d1d1f042474a840bbf0ef08d79b85144b5561d7397951e3`。
- Cobra 真实 Redis 只读探针（`2026-09-14`）得到 Q2 `5220/5220`、coverage `1.0` 但 `STALE/BEST_EFFORT_STALE`；readiness 为 `PARTIAL`，到期 Core 节点为 `AUCTION_0926`、`OPENING_0932`，artifact `575d9e5c152a39c023521210872f2824c335634915d71045df4888db20b711f5`。`engine-next` 与 `t1-v2-live` 保持 active、`NRestarts=0`。
- 该闭环只关闭 M1 的 side-effect-free startup assessment，不等于 Core 已接管 next 的启动生命周期；09:20/09:24/09:25 source freeze、正式报告/effect、跨重启 durable identity 与多日 differential 仍未迁移。详见 `docs/evidence/startup_readiness_probe_20260914.md`。

### 2026-09-14 legacy auction loader projection probe

- 对 Cobra 当前 release 的 `IntradayDataHub.load_auction_snapshots()` 做了受限只读复核：当前 Redis `0920/0924/0925` 各返回 200 行 TopN，600519 三锚点均存在，000001/000002 不在所读 TopN；无 guard write。该结果说明 Redis 0924 的存在随观察时点变化，不能把一次空捕获推广成永久缺失，也不能把 TopN 当完整市场集合。
- 600519 投影映射到既有 Core facts 后，两个相邻段保留 amount/pressure 观察，0920/0925 price=0 仍不升级为价格事实，shadow 继续 `PARTIAL/FACT_ONLY/OBSERVE`。未获得 AuctionState 或 runtime batch 证据，`FIRST_DIVERGENCE=UNPROVEN`。详见 `docs/evidence/legacy_auction_loader_probe_20260914.md`。

### 2026-09-14 Engine evaluation registration bound

- `DeterministicEngine` 的 evaluation 注册身份已改为有界 session ledger，默认上限 `65536`；不驱逐注册记录，容量耗尽时 fail-closed，确保 terminal tombstone 淘汰后不会重新注册同一 evaluation。commit `3d870ee` 在本地与 Cobra-ion Python 3.12.3 均通过 `375 passed`、`compileall`，详见 `docs/evidence/engine_registration_bound_20260914.md`。
- 该修复只关闭单 session 内存上界，不提供跨进程/跨重启 durable idempotency；Checkpoint/persistence 仍是 Core 替代 `engine-next` 前的后续门槛。

### 2026-09-14 Legacy/Core context read-path probe

- 已在 cobra-ion 对当前 `engine-next` release 与 Core `244e7ae` 执行 bounded read-only probe。旧 context builder 和 Core Q2 adapter 均无 Redis 写入；Core 5220/5220、coverage `1.0` 但 `STALE/BEST_EFFORT_STALE`，重复 observation hash 稳定。
- 旧 context builder 在指定 09:26 诊断中读到晚于该时间的当前 Q2，并将 `future_source_timestamp` 裁成 `CLAMPED_TO_ZERO_AGE`；这属于旧实时读取行为，不能作为历史 replay 的时间语义，也不迁移到 Core。
- 两个 probe 没有共享不可变 Q2 快照或同一 source-time as-of，跨链路 exact parity 保持 `UNPROVEN`，不能据此指定 first divergence。详见 `docs/evidence/legacy_core_context_probe_20260914.md`。

### 2026-09-14 Gate B morning fact dispatch

- 当前可执行 Core 提交为 `244e7ae`（状态修正 `b241a70`，typed-unit 修正 `ca05ddf`，功能提交 `41bc60d`，边界修正 `d976f6d`）。`dispatch_morning_fact_nodes` 仅把已到期的 `AUCTION_0926` 与 `OPENING_0932` timer evidence 组合到既有 Anchor/Opening facts；并在边界处拒绝跨股票或重复 tag，不新增 scheduler、workflow、retry、persistence、strategy effect，也不接管 engine-next。
- 本地与 Cobra-ion Python 3.12.3 使用同一归档均为 `373 passed`、`compileall` PASS。真实 Cobra opening differential 的 opening helper 对 100/100 exact；typed TD `chg_bp` 已按生产 `/100` 合同转换为百分点，可比 transition 99/99 exact，1 只真实空字段标记 `NON_COMPARABLE`，原因计数已写入产物。详见 `docs/evidence/gate_b_morning_fact_dispatch_20260914.md`。
- 真实 Q2 capture `q2_093210.jsonl` 为 5220/5220、coverage `1.0`，但 freshness/completeness 为 `PARTIAL/BEST_EFFORT_MIXED_FRESHNESS`；000001 的 09:26 Anchor 与 09:32 Opening dispatch 均 `READY`，600519 因真实竞价 price/change 缺失仍 fail-closed。该结果证明真实只读数据可进入同一事实链，不证明实时新鲜度或 engine-next 替代。
- 当前仍需先完成 production timer/batch 证据、0924/0925 source contract、engine-next loader/report parity 与多交易日 shadow；不新增 Provider、Rabbit consumer、Checkpoint、watermark 或 effect。

### 2026-09-14 morning vertical slice shadow

- 当前 Core 可执行对象为 `b902d7e99f1294dcffee275a15d17818ab7ae900`。新增的 `examples/run_morning_vertical_slice_shadow.py` 只是薄的、只读组合工具：复用 Q2 Adapter、既有 TD `auction_snapshot_v2` 查询和事实轮子，输出 Core-owned timer evidence、Q2 状态、AuctionFactShadow、OpeningFact 与 provenance；不启动完整 Engine timer 消费、不执行策略、不写 Redis/TD、不接 Rabbit、不发通知。
- 本地与 Cobra-ion Python 3.12.3 均通过同一归档的 `332 passed`、`compileall`；归档 SHA-256 为 `99d635c859e96782dab097609b1e30de0f30ae140dca15dcea0b0578f1744114`。固定时间同一 Q2 capture 重跑两次，artifact SHA 与 semantic hash 均一致，详见 `docs/evidence/morning_vertical_slice_shadow_20260914.md`。
- 真实 Cobra 只读运行得到 Q2 `5220/5220`、coverage `1.0` 但 freshness `STALE`；TD 返回 0920/0924/0925 三行，Auction shadow 为 `PARTIAL/FACT_ONLY/OBSERVE`，previous-day 与 hot-plates 因可用时间/metadata 合同未知保持 `UNAVAILABLE`。这证明真实读取和 fail-closed，不证明 Core 已替代 `engine-next`。
- 下一步停止扩展基础设施，进入 Gate B 与第一条已验证 Auction Shadow 规则；Engine timer 消费、engine-next loader/report parity、多日 Shadow 和替代前启动协调仍未闭合。

### 2026-09-14 Gate B anchor delta fact

- 新增 `AnchorDeltaFactV1` 与 `examples/run_anchor_delta_shadow.py`。它只迁移生产 release 中已审计的纯逐股 anchor delta：`0920→0924`、`0924→0925`；不迁移板块评分、leader、turn_strong、Strategy 或报告编排。最终代码为 `697b6ba2e5cfc2ad9dcda3d3851620071d3007fc`。
- Cobra-ion 对同一真实 TD `auction_snapshot_v2` 读入后，生产 `engine_next` helper 与 Core helper 的 canonical JSON 逐字段 exact compare 均通过；2026-09-09 两段均有真实数值，2026-09-14 因 price 缺失两段均一致返回 `unavailable`。最终本地/Cobra 套件均为 `351 passed`，详见 `docs/evidence/gate_b_anchor_delta_20260914.md`。
- 该规则已达到事实层 VERIFIED，但不改变 Core replacement 状态；下一步仍需 Gate B 状态生命周期审计和最小 Auction Shadow differential，禁止因事实函数通过而宣称策略已迁移。

### 2026-09-14 当前验证覆盖

- 当前可执行代码身份为 `541383d`；本地 Windows Python 3.9.13 套件为 `322 passed`，Cobra-ion Python 3.12.3 对同一归档也为 `322 passed`，`compileall` 通过。版本差异按用户决定仅作记录，不影响继续推进；正式运行证据仍以 Cobra 3.12.3 为准。此前本节中的 `306/307/311/313/315/320 passed` 及更早 commit 均为历史证据，不能覆盖当前提交；最新真实 readiness 证据见 `docs/evidence/reference_data_readiness_20260914.md`。
- 2026-09-14 开盘前对 Cobra-ion Redis 执行真实只读 Q2 探针，目标日期 active cohort 为空，返回 `MISSING/EMPTY_UNIVERSE`，不是 live coverage 通过。ground-truth capture 进程已运行但尚未到交易时段采样点。
- 2026-09-14 10:56–10:58 在 Cobra-ion 生产共享 Python 3.12.3 上完成真实 reference-data readiness 复核：热板 50 行、昨日涨停池 40 行均真实读取且 HLEN/scan 一致，但 metadata 均缺 `schema_version/available_at_ms/field_units`，按合同保持 `UNAVAILABLE`；同期 Q2 5220/5220、coverage=1.0，但 60s policy 下全量 `STALE`。该结果是成功的 fail-closed，不是 provider 失败；详见 `docs/evidence/reference_data_readiness_20260914.md`。
- 2026-09-14 11:10–11:13 完成当前 release 的 M0 启动/动作链只读审计：固定 Cobra release `20260903_e272842`、systemd 入口与服务身份，逐节点核对启动自检、09:20/09:24/09:25/09:26/09:32 调度及 getter/action 的读写边界；`load_auction_snapshots` 是只读投影，`recover_auction_anchor`、hot/yest fetch、mapping loader、market summary rebuild 可能写入或触发外部 I/O，Core 不能直接复用。当前 Q2 真实探针仍 5220/5220、coverage=1.0、全量 `STALE`；M0 结论为 `OBSERVED`，不宣称 Core 已具备替代启动协调。详见 `docs/evidence/m0_startup_flow_audit_20260914.md`。
- 当前仍不具备 `engine_next` 替代条件：缺真实当日 0920/0924/0925 成对证据、完整 source/runtime batch membership、正式报告 owner 和跨重启 durable idempotency。单 session evaluation 注册已有 65536 有界 fail-closed 合同，但不得据此宣称长期/跨进程替代已闭合。
- 2026-09-14 补充 M1 纯启动/重启矩阵：复用既有 `SessionPlanV1` + `SessionTimerV1`，只覆盖 Core 消费节点 `AUCTION_0926` 与 `OPENING_0932`；证明盘前无 due、09:26 正常触发、09:28 `RECOVERY_CATCHUP`、09:33 冷启动稳定补发、完成节点不重复、非交易日/跨日期 fail-closed。09:20/09:24/09:25 source freeze 仍由 t1-v2 负责；该矩阵不实现 StartupReadiness、持久化 exactly-once 或新的 scheduler。详见 `docs/evidence/m1_startup_restart_matrix_20260914.md`。

### 2026-09-13 23:50 最新收口复核

- 当前审计文档提交为 `8c499ca`；可执行源码自 `6275853` 后未改变，本轮仅补充真实证据和旧 consumer 路径审计。Windows 本地与 cobra-ion 生产共享 Python 3.12.3 临时归档均为 `306 passed`，`compileall` 通过，工作树干净。
- Cobra 真实 TD `auction_snapshot_v2` 只读 shadow（2026-09-10/600519/0920,0924,0925）两次重复执行：0920→0924 `PARTIAL`、0924→0925 `READY`，结果 `FACT_ONLY/OBSERVE`；artifact SHA-256 为 `e7a06126d3d9d89e5aeb66f6400771363d5a21297341ac2c111c55126d31c7d0` 与 `a9977c6fd994474bef5356bc4f0b7d7ddf01b2a35c0a152090e3d2f9cdc2ba41`。详见 `docs/evidence/real_td_auction_shadow_20260913_2316.md`。
- Cobra 当前 `engine_next` release 的 `build_auction_plate_bucket_stats`、`build_auction_snapshot_delta_stats`、`build_opening_validation_bundle` 调用及竞价 bucket 分支阈值已完成源码级只读审计；调用路径是 `OBSERVED`，实盘可达性、单位/生命周期和同输入 legacy oracle 仍是 `UNKNOWN`，因此不迁移正式 AuctionStrategy。详见 `docs/evidence/legacy_auction_consumer_active_path_20260913.md`。
- 2026-09-13 对当前 release 的真实 Redis 只读路径复核：`load_auction_snapshots(2026-09-10)` 返回空快照；`IntradayContext` 对三个指定 symbol 返回行但竞价金额/涨跌字段均为 `0.0`，最新行情时间为空，板块字符串在远端输出为 mojibake。Redis 写保护未触发；这些结果仅为 `OBSERVED/UNKNOWN`，不作为 legacy oracle 或迁移依据。详见 `docs/evidence/legacy_read_path_probe_20260913.md`。
- 当前不新增 Provider、Replay、Engine 或生产 writer。下一有效动作是下一个真实交易日获取受限的 Redis Q2/竞价 0920、0924、0925 输入并与旧 consumer 做同输入差异核对；在此之前继续保持 `AuctionFactShadow=FACT_ONLY/OBSERVE`。

### 2026-09-13 当前继续执行状态

- 当前可执行代码对象为 `ed3547c5799e6b72dcdcc79f1f13d500616ac0fa`。相对于此前的 `340b523`，仅修正真实 Redis opening probe 在空 `q2:active` 时的状态映射：`MISSING/EMPTY_UNIVERSE` 不再报告为 `FRESH`；并新增对应边界测试。该对象在本地仓库根目录和 `cobra-ion` Python 3.12.3 临时副本均为 `306 passed`、`compileall` PASS，真实探针 `--help` 入口也通过；远端复验使用 `TZ=Asia/Shanghai`、`PYTHONHASHSEED=0`。其后提交仅为证据文档，不改变可执行代码。
- 已用代码等价的 `340b523` 归档在 Cobra 生产共享 Python 3.12 环境重跑真实参考源探针：6/6 connector connection PASS；仅 BaoStock 日线同时闭合请求/返回交易日，其余来源仍为 OBSERVED，不能直接进入历史 runtime/replay。`ed3547c` 只改变空 Q2 的报告映射，未改变任何 connector probe 代码。
- 已用 Cobra 生产审计目录中的真实 Redis 捕获文件重跑旧窄读取器：2026-09-07 的 0920/0925 各 200 行、无重复、无写入；该证据不是 live retention、完整市场快照或 0920→0924 相邻段证明，0924 当前仍 UNAVAILABLE。
- 历史仓库中已存在 `v0.3.1-engine-integration.1/.2` 标签；它们指向早期验证文档提交，不移动、不删除，也不把它们解释为当前生产链验收。当前可执行对象 `ed3547c` 尚未创建新的正式验收标签：仍缺真实 0924 相邻捕获、Rabbit/runtime batch membership、Redis/TD writer projection 的上游一致性证据及旧正式 consumer oracle。下一步优先补证据，不新增 Provider/Replay/Engine 框架。
- 2026-09-13 同提交真实只读复核：TD `daily_kline` 返回 2026-09-10 的 600519/000001 两行，但 `available_at` 未知，结果按合同为 `UNAVAILABLE`；TD `auction_snapshot_v2` 的 600519 三锚点可重建 `PARTIAL/FACT_ONLY` 事实；周日 Redis Q2 为空，adapter 返回 `MISSING/EMPTY_UNIVERSE`，不视为 live coverage 通过。

- 新内核代码基线：663743cbebac5eafd9532c7fc3967b7df7307ea5。
- 2026-09-09 07:15 Asia/Shanghai 远端只读核对：生产 engine-next 为 e272842c8f490f55a1b017badb71e71904ce008e，路径 /home/exedev/services/engine-next/releases/20260903_e272842。
- engine-next 与 t1-v2-live 均 active；根磁盘可用约 4.8GB。这是瞬时观察，不是持续可用性证明。
- 父仓库本地 HEAD 67822fd 与生产不同；不能以本地旧链直接断言当前生产行为。每个迁移项必须绑定 production source commit。
- 2026-09-09 07:16 只读 Redis：q2:active:20260909 和 q2:active:20260908 的 SCARD 均为0；q2:600519 仍有 ts=1788748654000 的旧记录；cache:yest_limit_pool:20260908 不存在；market:stock_plate 为 hash。空 active 集合尚未区分不存在/空集合，不宣称全天行情故障。
- 相邻任务 01a066b5-5852-7a00-a5fd-f07da48360cb 已受托只读总结。它也维护 engine_core；本次要求不编辑代码，避免并发修改。
- 曾有真实 Redis/TD/BaoStock 证据；不能称全部测试为合成数据。当前不足是持续真实输入、生命周期和正式消费者闭环。

## 目标与边界

t1-v2 继续消费 RabbitMQ 并生成 Redis 原生行情。新内核以 Redis Q2/Auction 协议为行情入口；第三方参考数据通过明确日期、来源和可用时间的准备结果接入。

迁移既包含真实数据，也包含任意时刻启动、自检、按需补齐、事件处理、持久化、事实分类、报告及回放。每项按实际消费者需要接入，不先实现通用平台。

每个数据集记录：source、getter、writer、reader、业务日期、observed_at、可证明的 available_at、完整性、单位、持久化位置、补齐条件、回放支持。当前抓到的历史数据不反推历史可知时间。live 新抓取可记录从实际接收起可用，不能伪造 source 首次发布时间；这需要显式合同审计。

## M0：生产数据与动作清单

逐项阅读当前 release 的 getter 和调用者，补齐以下清单；当前仅确认函数存在，未全部验证调用成功。

| 数据 | 生产入口/边界 | 首批验证 |
|---|---|---|
| 原生 Q2 | q2:active:{date}, q2:{symbol}，RedisQ2ProjectionAdapter | 日期、字段、单位、source timestamp、缺失、读取一致性与成本 |
| A2/Anchor | 生产 snapshot/anchor writer 与 reader | 完整集合和 TopN 区别、tag、freeze 时间、source state |
| 昨日涨停/连板 | KaipanConnector.fetch_yesterday_bans_pool(trade_date) | 实际传入日期、cache meta/hash、空池与缺失、lb_days |
| 热板/原因 | fetch_hot_plates(trade_date,today_mode), fetch_ban_reasons | 当日/历史模式、板块键与股票键、是否写回 |
| 日历/日线 | BaostockConnector.fetch_daily_kline / fetch_daily_kline_range | 显式日期、交易日、完整性、增量/断点、TD持久化 |
| THS热度 | ThsHotConnector 与实际底层 API | getter、当前/历史能力、采样时间、缓存与刷新周期 |
| 问财 | WencaiConnector 与实际底层 fetcher | 查询原文/日期、请求成本、正式真值时机、落库 |
| 身份/Mapping/市值 | F10 与 frozen mapping /现有市值 authority | 单一owner、日期、名称降级不污染行情 |
| 非原生缓存 | factor、prior、mapping、统计投影 | writer/reader同口径、日期、失效、需不需要新内核消费 |

不能仅凭函数名假定无副作用；health_check、fetch、repair、load 都需检查真实调用。

M0通过：形成 current release 的 source/action 表，确定首个消费者及必须字段。Redis 缺字段时先追 producer→writer→reader；只改首次丢失位置。

## M1：真实 Redis 到新内核

使用已存在 RedisQ2ProjectionAdapter、MarketStateReducer、Engine。先有限次只读运行，保留观察时间、source时间范围、请求/收到/有效/跨日/缺失数和semantic hash。盘前无当日行情是合法状态，旧quote不可冒充当日。5000+逐股HGETALL的读取成本需实测；必要时复用Redis pipeline，不另造访问框架。

在同一批捕获输入上重复计算验证确定性；两次实时读取可因行情变化而不同，不要求实时输入hash相等。当前market cohort不能冒充历史cutoff完整快照。

通过后证明一个明确的事实消费者可用：优先竞价P/M/RB/RA/pressure端点差。现有anchor direction/withdrawal/ratio和新Segment标签不等价，分别列出保留与未迁移字段，不直接替换整个对象。

## M2：真实参考数据与准备链

顺序：日历/Mapping/F10 → 昨日涨停与连板 → 昨日日线和确有消费者的市值字段 → 热板/THS/问财实际需要的数据。

先读取既有Redis/TD结果；缺失时在允许时段调用既有owner补齐。按dataset/date/scope去重，有界请求和重试。准备worker可阻塞，reducer不可阻塞。参考数据失效要明确原因，不用更多fallback掩盖。

每个源至少有一次真实请求/缓存读取证据、一次日期核对和实际消费者输出。不能以mock或connector存在认定接入完成。旧源适配属于外部组合边界，新内核不得import旧工程。

## M3：任意时刻启动与事件

先对当前生产代码核对时间，不从旧交接文档复制常量：盘前自检、08:30/09:00检查、09:15切阶段、0920/0924/0925源freeze、09:26报告、09:30、09:32实际cutoff、收盘/17:40准备。

每个动作保存 scheduled_time、actual_time、trade_date、前置数据、执行owner、缺失处理与去重依据。晚启动补数据和补执行分别判断；当前quote不能恢复过去的cutoff，缺历史时明确不可重建。Timer不伪装Tick。

按盘前、竞价中、09:25后、09:32后、午休、盘后六类启动验证。只有确实需要跨重启不重复的动作才增加最小持久执行记录，优先复用现有dedup。先输出拟执行动作，再逐个验证真实补齐与写入。

## M4：事实消费者与报告

接入市场、canonical plate、昨日涨跌停反馈、连板高度、大市值、个股极值及风格。身份、市值、风格必须先证明已有authority/定义；无既有定义不自造分类分数。

Auction和Opening优先复用既有正式事实与报告合同。将迁移计算以薄组合边界提供给消费者，固定名称、单位、denominator、missing及status。先manual_audit生成HTML/Text/EML，核对同源事实与源数据；再验证正式发送owner/dedup，仅一个owner发送。

## M5：同链回放

上一完整交易日09:15–09:40；优先现有P1 Q2Frame/TD→t1-v2回放产物恢复Redis等价视图，之后复用相同新内核与消费者。

新内核的TD event-time回放不得被误称为重新执行t1-v2算法。重建原生Q2应走已有t1-v2计算；无法恢复字段留缺失。静态输入按目标日期准备，run不调用当日HTTP，不写生产，不发送邮件。

验证两段报告输入、时间推进、空窗、cutoff、防未来数据、重启去重。按相同输入粒度比较事实；不要求legacy Rabbit批次等价。

## 上线顺序和停止条件

各阶段：最小patch → 单测 → Linux真实证据 → clean commit → 最终commit复验。

先实盘数据只读运行，再默认关闭的消费者shadow，再单项owner切换。生产依赖用固定版本制品，不用sys.path临时拼接、源码复制或subprocess桥接。唯一迁移职责验收后才移除旧实现。报告发信、持久化、运行owner分别验证，避免双发送/双写。

当前阶段：M0仍在补齐 current-release source/action authority；M1已有受控真实读取与消费者证据，但尚未形成新版本完整验收；M2正在推进昨日涨停/连板准备链，producer contract 已完成 candidate/integration，仍阻塞于 Cobra runtime metadata 与真实 Shadow evidence；M3–M5待执行。既有测试数量不代表上述迁移阶段通过。

## 已验证可执行的只读命令

PowerShell，通过8878连接cobra-ion：

```powershell
python D:\work\Go\tools\ssh_broker.py --port 8878 status
python D:\work\Go\tools\ssh_broker.py --port 8878 run "date -Is; readlink -f /home/exedev/services/engine-next/current; cat /home/exedev/services/engine-next/current/RELEASE_COMMIT; systemctl is-active engine-next t1-v2-live; df -h /"
```

本地基础验证（cwd必须是独立仓库）：

```powershell
git -C D:\work\Go\engine_core status --short
git -C D:\work\Go\engine_core rev-parse HEAD
Set-Location D:\work\Go\engine_core
python -m pytest -q
python -m compileall -q src tests
git diff --check
```

正式验证使用服务器Python3.12固定提交副本。M1 目前已有只读 Q2/Opening runner；M2 prepare 与 M4 build-only 命令尚待实现，当前不虚构尚不存在的CLI。服务器没有redis-cli，使用既有venv的redis客户端，不为探查安装新服务。

M0 只读转换/兼容命令已固定在 `engine_core/examples`，后续优先重跑命令，不修改业务 reader：

```powershell
Set-Location D:\work\Go\engine_core
$env:PYTHONPATH = "src"
python examples/run_real_cache_inventory.py --trade-date 2026-09-10 --previous-trade-date 2026-09-09 --output C:\temp\cache-inventory.json
python examples/run_real_calendar_probe.py --start-date 2023-12-01 --end-date 2026-12-31 --version baostock-YYYYMMDD-v1 --declared-from 2024-01-01 --declared-to 2026-12-31 --output C:\temp\calendar.json
python examples/run_real_f10_inventory.py --csv C:\data\f10.csv --provenance C:\data\f10_provenance.json --output C:\temp\f10-inventory.json
```

三条命令都使用独占输出、UTF-8/结构校验和 SHA 身份；cache 先 `TYPE` 再解析，calendar 拒绝空响应/日期缺口/重复行，F10 对现场 `code.exchange` 格式做显式白名单校验。它们产出的是准备/审计证据，不是 runtime writer，也不会把 UNKNOWN 的金融 as-of/单位转换成可消费事实。

## 2026-09-11 继续推进记录

- M0 Q2 source/action inventory 已完成一条真实证据：Cobra `q2:active:20260910` 为 5219 个 symbol，`engine_core` exact baseline `f71572bcecdbddad2157505382900e7051124681` 通过 Redis `SMEMBERS + HGETALL` 读取并进入 core engine；结果为 `1.0 coverage / 0 missing / 1 stale / PARTIAL`，同一 observation 重算两次 hash 一致。证据见 `docs/evidence/m0_q2_real_source_action_inventory_20260911.md`。
- A2/Anchor 已核对：`market:auction:{date}:{0920,0924,0925}` 是 hash，`market:auction:anchor:{date}` 是 JSON string，`market:auction:{date}:latest` 是 hash；后续兼容脚本必须先 `TYPE` 再解析。误用 `HLEN` 的 `WRONGTYPE` 已保留为 dialect 证据。
- 当前仍不能宣布 M0 完成：日历/Mapping/F10 的 authority 仍未闭环，昨日涨停/热板/原因虽已观察到带日期的 Redis payload，日线/因子也已观察到 2026-09-08 至 2026-09-10 的完整日期分区，但 producer→writer→reader 的全链、watermark/cache 一致性、F10 市值时点和 THS/问财历史合同尚未逐项形成验收证据；也不能把上一交易日缓存观察当作当前盘中 fresh。
- 参考数据准备链已做一次有界真实探针：六个 connector 连接均 PASS；只有 Baostock `fetch_daily_kline(2026-09-10)` 达到请求日期与返回日期均明确的合同 PASS，开盘啦/同花顺/问财结果均保留为 OBSERVED，原因是响应未自带历史日期或接口没有日期参数。artifact `cc-m0-reference-20260911.json` SHA-256 为 `ed22790afa0d092ce22a7378ee7e62c892f70a065a78ea67aa8f36e65c64ac08`，详见同一证据文档。
- 日历/Mapping/F10 现场 inventory 已完成但尚未达到 core authority：core 已有可验证的 BaoStock `TradingCalendarSnapshotV1` 候选（748 canonical dates），当前 Cobra BaoStock 查询复核了 `1127 raw rows / 748 trading dates` 的计数口径，但尚未证明制品已交付 Cobra runtime；legacy 仍以 `holidays.CN()` 推导。三个 mapping hash 覆盖数不同，`config:plate_mapping:info` 与 `config:plate_mapping:full_sync_info` 均不存在，无法确认当前 mapping 批次身份；F10 CSV 的 file hash 与 provenance 已闭环，legacy service 也会用 `_source_sha256` 拒绝陈旧 cache，5574 records 中 77 条仅有旧名称且 financial fields unavailable，但仍缺目标交易日 as-of/单位合同，且 cache miss 会写 Redis。证据见 `docs/evidence/m0_calendar_mapping_f10_inventory_20260911.md`。
- 日线/因子/筹码现场交叉样本已补齐：Redis 2026-09-08 至 2026-09-10 的 `stock_extra/chip_peaks` payload 日期全部精确匹配，TDengine `600519` 三个日期的 `daily_kline/daily_factors/daily_chips` 均可读；但 amount 存储精度、volume=0 和因子多项为 0 等语义问题尚未完成全量质量验收，不能直接接入 core。可复核的 Redis inventory v4 artifact SHA-256 为 `4296c7b6788b9b3ae93adb9f5b14977538879570257b36ff38c32207209b1e61`；v4 还确认所有扫描 hash 的 HLEN 前后与扫描项数一致，并纳入 `config:plate_mapping:full_sync_info` 检查。
- M1 的受控代码身份任务已完成一次真实 Cobra Shadow，但 Acceptance Bundle 仍停在人工生产 Gate；它不改变生产 release，也不等于整个 next→core M1 完成。
- M1 已补一条真实 Q2→Opening facts 消费证据：runner `run_real_opening_facts.py` 在 `2026-09-10` 分区上完成 `5219/5219` cohort、`0` missing 和 3 个选定 symbol 的纯 core fact 构建；但采用 `60s` freshness policy 后整体为 `STALE / BEST_EFFORT_STALE`，artifact `cc-m0-opening-facts-20260911-v1.json` SHA-256 为 `31fafe45ab0d36674e081abe857a048abc1b84c2e2b5bd0c7132c383f0d812df`，所以只证明消费者链和 stale fail-closed 语义，不证明实时 cutoff。
- 日历只读探针已在 Cobra 当前 Python 环境完成一次唯一执行：`1127` 条 BaoStock raw rows、`748` 个 canonical trading dates，raw source hash 为 `399ce4aa1413c8824a57c1a55f18c0f3d42ac1d7c969d9aaec81a5cf143b9ce4`，与 core fixture 的 trading-date 集合完全一致；artifact `cc-m0-calendar-20260911-v3.json` SHA-256 为 `b62dbb28a0a945297e4035627d137e51229e7528d8ac2e9b8ca37d41f28debe5`。探针已增加严格日期、全覆盖、重复、空响应和 flag 校验，但这仍是 authority 候选复核，不代表已交付 Cobra runtime。
- F10 只读 inventory 已完成唯一一次 Cobra 执行：脚本明确支持现场 `6位代码+SH/SZ/BJ后缀` 格式并拒绝静默去后缀；`5574` 条记录、`5574` 个唯一 code、`77` 条 identity-only，provenance 与文件 SHA/行数/名称计数/重复码/identity-only/new+identity 计数全部一致。artifact `cc-m0-f10-inventory-20260911-v4.json` SHA-256 为 `27eab8615f2286f39713778029a78ca45829e514278c3c0d7925edb90989ede9`；只闭环文件身份与结构，`core_ready=false`，金融字段 as-of/单位仍 UNKNOWN，未调用 Redis/TD/F10 service writer。
- 另确认一项真实单位兼容缺陷：`chip_batch_runner` 写入的 `real_market_cap` 已是亿元（现场样本 `600519=16245.06`），legacy context reader 原先又按元除以 `1e8`。本地已改为显式 `source_unit=yi|yuan` 并通过 `12` 个 context/unit 测试；该 patch 尚未部署，当前 Cobra release 仍需按 candidate→trusted verify→人工 Gate 流程处理，不能以本地测试代替线上修复。
- 该单位修复已首次走完受控 candidate/integration 链，但尚未进入 Cobra Shadow：确定性配方 `engine_next.real_market_cap_units.v1`（recipe hash `3491f2e5640feeee52bcdcf719d61f94d6e77660aea3447c809ca95a8151a879`）在独立 Windows CRLF clone 中只修改 `engine_next/runtime/intraday_context_builder.py`，candidate `de99989e73b6050076d9380c12bf73e0e41da98b`，integration `35c4d165169bec82a1009d69ea368f870925bb4c`，两者 tree hash 均为 `e98fb40ed391e2d0142d86ccd985b72af13a9b5c`。trusted verifier 对两个 exact commit 均通过（candidate manifest `937b750369876539bb7aa276e2f7d12c95b4f079d3fb1aabba410fa22eeade8e`，integration manifest `c819acdc55d8907302ca126bec458bd19203dd4c670d8d0310afa4bf113d16af`）；Luna/low 独立审计均 PASS 且无 findings。首次集成因隔离 ref 缺失进入 `IN_DOUBT`，补齐预期 base 后由同一 operation 恢复成功，未重复集成。当前控制平面状态为 `SHADOW`，因为该 patch 尚未部署到生产 release，不能伪造 Shadow/Acceptance Bundle，也没有做生产写入。
- 转换器同时补齐了 Windows CRLF 对多行字面规则的适配：配方仍以 LF 内容寻址，匹配/写回采用目标文件换行风格；`tools/tests/test_cc_transform.py` 为 `4 passed`。转换脚本不执行 shell、网络、模型或业务判断，后续同类兼容问题优先新增配方并在隔离 clone 重跑。

### 2026-09-11 受控兼容任务继续推进

- q2 竞价字段兼容已登记为 TaskSpec `efcf5d05f894dc40255674216d740e1a0a66bb3b97435340cb2c7dd9a6cc23ed`：确定性 recipe `engine_next.q2_phase_compat.v1`，candidate `97701e2edc454499ba9e3b0a0cce144d0efa3439`，integration `4501ac931dd5b9255716d36f66e78633dc8cc5ba`，candidate/integration tree 均为 `216871ff6cb7ef879f48cfdd08d960b81a0f9755`。缺失 `ph` 与显式非竞价 `ph` 的 exact candidate 行为测试通过；candidate verifier `1aa9e8c23a3475fae908d34643529589d7a519004d3c0885ec5311030db30eb4`、integration verifier `cd232980fced11012b1b923f3937a49c8722536df6a28abbad94234efb994357`，Luna/low candidate 与 integration 审计均 PASS。当前状态为 `SHADOW`，未生成 bundle、未部署。
- 股票画像风格提示首个 candidate 已登记为 TaskSpec `ec57102b909d0e1092b1ca153e048b9a12b79a28ba72c82a3e83ac9cd80a7793`，candidate `db8d450739e7df5fa8e0aaa36a322adac38788e5`，只修改 enum 与 profile 两个允许路径；exact candidate 行为测试和 trusted verifier `732824c9edcebde9f3ad15b42fb763164cf0066db6c377699ca614a4cb89d7e2` 通过，Luna/low 独立审计 PASS。该 candidate 后续因集成审计发现 precedence 问题而被 repair TaskSpec supersede，不能作为最终 acceptance 身份。
- 上述股票画像在集成审计中发现了真实 precedence 缺陷：`INSTITUTIONAL_TREND` 先于 `OVEREXPOSED/CHASE_RISK` 返回，导致机构趋势画像可能绕过 `AVOID_CHASE`。失败审计没有被接纳，任务进入 `REPAIR`；第一轮修复验证因容器缺少 pandas 且包入口加载无关 context pipeline 被分类为 `NONRETRYABLE_INFRA/BLOCKED`，没有覆盖代码结论。随后以 repair TaskSpec `11882ffee454d12213fe5a2ac50f4340b39ef7a20969d1f336aba34851e9924c`（baseline `db8d450739e7df5fa8e0aaa36a322adac38788e5`、context v3）重新执行；precedence recipe hash `86678f305319d068a6a506e92abbc19e6a83c8706e7c3b71d8b570a6b04b21e2` 产生 candidate `12b57bf0a9837c6845e58c2cf348d94d9ee69e4e`，integration `0e74a7f0ed9877eb912c5bc11f2528a12d2d5d30`，tree `be8896332197d7eb6f987f0ba82e17463b89536d`；trusted verifier `c4817183e1a931fb28e8cd6be893acffd2085ec17a9687daab03b0b38db05c77` / `efcafff804a358e1a29b5f878b9c6d0d010de2737c59104e584ebf5043878d33` 与 Luna/low 两次独立审计均 PASS。当前状态为 `SHADOW`，未生成 bundle、未部署。
- 转换器新增 `postcondition_authoritative`，补上插入型 literal rule 的幂等语义；旧 recipe 的默认 false 不改变既有内容 hash，`tools/tests` 当前为 `42 passed`。同一控制平面 checkpoint 已验证：schema `3`、last event `75`、DB backup SHA `163a9d338dac19e1602fc929b236137217107422cd33cbac26621162e4638770`、event journal SHA `ede86b6937caf1c850ee8d4cdfd25effbfb329ac5f2e0b1c4a230cb7bf56c0d7` 均一致。并发写状态目录时第二实例被 OS lock 拒绝，证明单写者边界生效。

### 2026-09-11 符号身份兼容收口

- 新增 `engine_next/domain/symbol_identity.py` 作为统一身份边界：仅接受裸 `######`、`SH/SZ/BJ.######`、`######.SH/SZ/BJ` 以及对应的无点号紧凑格式；未知交易所、短码、长码、任意前后缀和不可解码字节均拒绝，不再通过 `split('.')`、`[-6:]` 或抽取数字制造伪合法代码。
- `intraday_data_hub`、`intraday_context_builder`、`app_main`、startup/static mapping、Rust snapshot/feed、tick tracker、recap、Baostock/开盘啦/同花顺/问财 connector、runtime kline/chip adapter 和 DDE sync 已统一使用该边界。行式输入的非法身份只会被跳过；需要构造外部请求的 DDE/Kline/Baostock 边界使用严格异常，避免生成空 Redis key 或错误请求。
- 生产代码扫描确认 `engine_next` 已无剩余 `split(".")` / `[-6:]` 符号截断点；测试代码中的历史 fixture 辅助截取未作为运行时身份逻辑。
- 专项验证：`test_symbol_identity + connectors + Rust + Tick + chip + recap` 为 `36 passed, 5 skipped`；Context/Hub 为 `25 passed, 1 deselected`；新增及控制面 `tools/tests` 为 `42 passed`；全部 check 文件为 `61 passed`；符号静态 Gate 扫描 `98` 个生产文件、违规 `0`；`compileall` 与 `git diff --check` 通过。
- 这只是本地身份合同与数据入口收口，未部署 Cobra、未改变生产 release、未生成生产 Acceptance Bundle；下一步仍需按 candidate→trusted verify→independent audit→integration exact commit→shadow 流程推进。

### 2026-09-11 M1 竞价 Redis/TD 投影比较

- 通过 `cobra-ion` broker `8878` 对 2026-09-10 的 3 个样本、3 个竞价标签执行只读 Redis/TD 比较：TD 返回 9 行，时间 `MATCH=9/9`，共享 amount/bid 字段无 mismatch；三个 Redis `top_amount` 均为 200 行，selected 缺失明确归类为 `symbol_outside_redis_top_amount_window`；结果为 `PARTIAL_COMPARABLE=4`、`NOT_COMPARABLE=5`，artifact `cc-m1-auction-projection-20260911-v4.json` SHA-256 为 `367c2979a26d86c93504028316a428a3a5dac6b5552122584ec6123e9d16e5c2`。
- 比较器补齐 TD driver 带时区字符串的 epoch 解析，单测 `7 passed`；Redis 0920/0924 selected rows 缺失、0925 anchor 缺少 ask 字段，均按合同保留 `NOT_COMPARABLE`，没有用 0 或 TD 值补齐。
- 该证据只证明部分 projection 与时间身份一致，不能宣布竞价投影全量等价，M1 仍为 `PARTIAL / NOT_READY`，未部署、未生成 Acceptance Bundle。

### 2026-09-11 M2 昨日统计真实读取

- 通过 `cobra-ion` broker `8878` 对 `000001`、`000002`、`600519` 执行 2026-09-10 的 TD `d_<symbol>` 日线只读查询，并由 core `PreviousDayStatsFunction` 处理；三行均返回且请求/实际日期一致，实际来源为 `tdengine_daily_kline`。
- core 结果严格为 `UNAVAILABLE`、completeness `0.0`、`missing_fields=["available_at_unknown"]`，不是数据读取失败：TD 现场没有历史 `available_at` 证据，不能用本次 `observed_at` 代替历史可知时间。artifact `cc-m2-previous-day-20260911.json` SHA-256 为 `6ee786f77647ad6c8024d4d77883d328866393c36945e090e81940451cb0c973`，core content hash 为 `84dd2ecd4ff635181b1b8c416b6e690de34d0a74409bb7b0873a7b441b080bf0`，详细证据见 `docs/evidence/m2_previous_day_stats_20260911.md`。
- 本次只证明真实 TD 读取、日期身份和 fail-closed cutoff 语义；volume=0、amount 精度、watermark/单位及历史 replay 可知性仍未闭环，不能把该结果接入生产决策或宣布 M2 通过。

### 2026-09-11 M2 昨日涨停池真实读取

- 通过 `cobra-ion` broker `8878` 只读读取 `cache:yest_limit_pool:2026-09-09`：Redis type 为 `hash`，`HLEN/HSCAN=48` 且扫描稳定，48 行 JSON、symbol field 和 `trade_date` 全部一致；meta 为 `kaipan/postmarket`，`fetched_row_count/cache_row_count=48`，`updated_at=2026-09-10 17:40:10`。`lb_days` 分布为 `1B=33、2B=10、3B=4、5B=1`。
- 新增 `PreviousDayLimitPoolFunction` 与只读 Redis provider，在 core exact baseline 上对这 48 行执行日期、身份、字段和历史 cutoff 验证；结果为 `UNAVAILABLE`、`completeness=0.0`、`missing_fields=["available_at_unknown"]`，content hash `6a2f2592a3ea3dbd719e2e358f99b20bf28d4bd59d7ee6b49049817b3105ed57`。v2 曾发现 Windows LF→CRLF 落盘导致 hash 记录不一致，已修复 runner 写入边界并重跑 v3；权威 artifact `cc-m2-yest-limit-pool-20260911-v3.json` SHA-256 为 `c531572bf750e40506b937b3c3373bf018f3ba4f8d3f74ec28e0cd396033b1c3`，详见 `docs/evidence/m2_previous_day_limit_pool_20260911.md`。
- 现场发现 legacy schema 把 `turnover` 标为百分比，但真实值是数亿量级，meta 没有单位字段；core 保留原值并标记 `turnover_unit_unknown`，不做静默换算。另发现旧 producer `ai/API/StockAnalyzer.py:get_history_bans_pool` 仍有 `str(rec[0])[-6:].zfill(6)` 的 lossy symbol 解析；本次 core runner 未调用该 API，故 producer identity authority 继续 BLOCKED。该数据目前只证明真实读取和 fail-closed 语义，不进入 replay/报告/生产决策，也不能宣称全市场覆盖。
- 针对 producer 截断点新增可复用 recipe `stock_analyzer_symbol_identity_v1.json`；隔离副本 candidate `920548c207b2b9ffd9dc131490cc7e612995af80` 仅修改 `ai/API/StockAnalyzer.py`，recipe hash `6aa9589bb869edd9444f2147566da40ee3b0883e2c92c7de292d352b193b0155`，静态 Gate 从 1 个违规变为 0，重复检查为 `ALREADY_APPLIED`。该结果尚未进入 trusted verify/independent audit/Cobra Shadow，因此 producer identity authority 仍 BLOCKED。
- Cobra 对 `getHisBans(2026-09-09, ban=1)` 的原始 33 条记录复核确认：`rec[9]` 为 75.5M–2.87B 的整数成交额量级，`rec[14]` 为 6.41–53.43，`rec[22]` 为 9.89–10.14，`rec[2]` 全为 0；因此已新增 `stock_analyzer_ban_field_mapping_v1.json`，目标是 `rec[9]→turnover(yuan)`、`rec[22]→close_pct`，并同步 connector schema 单位。该 recipe 仍需在完整隔离 candidate 上 verify/audit；旧缓存没有 schema version/available_at，不能直接升级为 core acceptance。
- 两个 producer 兼容 recipe 已在最小隔离 clone 中串联验证：身份 recipe 输出 `fa54c94b69926af354f612d5c536c56c454cb800`，字段映射 recipe 输出 `517cb1ee49e44f2f323586adf23d36f7b2960d18`，最终 tree `b663f336e3f1dd52ec72179b471cfdacd7b022fa`；身份/字段 recipe hash 分别为 `6aa9589bb869edd9444f2147566da40ee3b0883e2c92c7de292d352b193b0155`、`8e69156c48c02b3a04e329a6ac2df71d6dc6eb28e629ea1e23ed51d7a296c6c5`。两文件编译通过，静态身份 Gate 为 0 违规，重复执行均为 `ALREADY_APPLIED`；尚未进入完整 production checkout、trusted verify、独立审计或 Cobra Shadow。
- 随后在完整仓库 `HEAD=67822fddc762cc82e75e0be6fded1bd7d49f6bfa` 的干净 worktree 中再次串联：身份输出 `a6eef590ef597a01aa1edcb1a7f4d6726cbfafbe`，字段映射输出 `de8c1dd1fff8a2f8e93f708421dd2de1aef56e61`，最终 tree `9b9a0aeb043a0205127090cf390d7cbdc9f8a888`，两个目标文件编译通过且 worktree 干净；但旧 HEAD 的全量 `engine_next` 静态扫描仍有 10 个历史截断点。因此该 recipe 链只证明 legacy producer/schema 的确定性转换，不宣称旧 HEAD 的全量身份 Gate 通过，仍未进入主树 candidate、trusted verify、独立审计或 Cobra Shadow。

### 2026-09-11 M2 producer 严格合同收口

- 针对前述旧 producer 的身份截断、`rec[9]/rec[22]` 默认值和 schema unit 错误，冻结确定性 recipe `legacy.stock_analyzer_ban_contract.v2`，最终 recipe hash 为 `47d1f417b35e9e720121e56e81393e04a39de19cc68901a44a0e0ed708839260`。recipe 只允许修改 `ai/API/StockAnalyzer.py` 与 `engine_next/connectors/kaipan_connector.py`，并增加严格 code/name/lb_days、非空数值、有限数值及 `turnover=yuan` 合同；不做网络、模型或生产写入。
- 该 recipe 在 identity integration `66bb9977fd08ae0236d3d4737182cccea9871aaf` 的隔离 full worktree 中生成 candidate `e9c832d31bae1edb1c6578d2ecdc7c51bc768c4c`，tree `47d015f5354afa8b4e202577a07d9f627b0a23cb`，transform manifest hash `5190f855980ca547deca081ada5460a5954da2d56c96531fbb51c2dcad003bc0`，外部 manifest SHA-256 `39e6e774b78c763ba2ff0e5ad8048f1c62b2fd22b4090a5a4058671c6ad515da`。TaskSpec `6825e724e49feb52e106ec65d6282fc7c9f93f6be4b1de19bb9a5fa3c170f280` 的 exact candidate trusted verifier manifest 为 `250c729fd97c6d42d31289287d23294c13a7f284cb55b69101b5a2e7fb8ceba2`。
- 按 expected base 严格集成得到 `313222c5e6d7f8abb632c83b733904a43542fd54`，integration tree 与 candidate 一致；integration verifier manifest 为 `5725cc1f72eaf77cae0567e4455b7cb61741af53bb75d31b19495a74bc14a97b`，candidate/integration independent audit 均 `PASS` 且无 findings，audit hash 分别为 `55b1333f3273a755985e79656a3fce02d6f58f34aed90a79aeb9b7218f9c50e1`、`3cbfff71c707082b3c39a6a8bc6c9a9650a3421e06520084909bfe173b00853f`。R7/R8 审计失败保留为不可接纳历史，分别暴露 `lb_days` 强制转换、`ban_lvl` 回退与 `str(None)` 名称问题；这证明 repair→verify→audit 链真实拦截了伪合法数据。
- 当前控制平面状态为 `SHADOW`，只表示 candidate/integration 代码链通过；producer 尚未部署到 Cobra release，没有 Shadow 数据验收、Acceptance Bundle、生产写入或 release 切换。M2 仍受 `available_at_unknown`、`turnover_unit_unknown` 与 `producer_runtime_not_deployed` 阻塞，不能宣布昨日涨停池进入 core acceptance。
- 已通过 `cobra-ion` broker 对当前 Cobra release `e272842c8f490f55a1b017badb71e71904ce008e` 做 writer 只读复核：`fetch_yest_limit_pool` 当前只写 `updated_at/updated_at_ts/last_attempt_at/last_attempt_at_ts` 等 cache 更新字段，没有 `available_at` 或源字段单位元数据；`cache_preserved` 还可能复用旧 payload。因此不能把 writer 更新时刻冒充历史数据可知时间。下一步只允许先补 source/writer contract（可验证 `available_at_ms` + turnover unit），再走受控 candidate 链；当前不部署、不写 Redis、不生成 Acceptance Bundle。
- 已补齐 core runner 对显式 Redis meta 的适配：只有同一 meta 的 `schema_version=PreviousDayLimitPoolV1` 才接受 `available_at_ms` 与 `field_units`，旧 `updated_at*` 不会被推导为 availability，孤立/非法 meta 直接 `ERROR`；专项/全量测试为 `12/253 passed`。随后在 producer-contract integration `313222c5e6d7f8abb632c83b733904a43542fd54` 上执行 writer recipe `engine_next.yest_limit_pool_meta.v1`（hash `6c1767369769fc78339fa054f899de76c0e5b5fe288d92223432c889696860a9`），candidate `91f030addaa550f8205351ca450ffd56815ba4d5`、integration `6827d6dfe007e4699a9a60f1bd476ea22c727c64`，tree `4f1fa362da3e0abb87182f01cb82b444388912e6`。trusted candidate/integration verify 与 Luna/low 独立审计均 PASS；本地 fake-Redis 已覆盖 fresh/preserved/legacy 三路径。任务状态为 `SHADOW`，没有部署、生产写入或 Acceptance Bundle；M2 仍必须等 Cobra 现场实际运行该 writer 后，重新生成带真实 `available_at_ms`/单位元数据的缓存证据，才能继续 core acceptance。
- 为排除 writer candidate 自身回归，已在 exact candidate 与其 `313222` integration baseline 上运行相同的 `engine_next/tests` unittest：两者均为 `Ran 96 tests`、`8 failures / 26 errors`，结果完全一致；candidate 只改 `engine_next/runtime/intraday_data_hub.py`，`py_compile` 与 `git diff --check` 均通过。因此这些旧测试红灯暂归类为 baseline 已存在的接口/编码环境问题，不能作为本次 writer metadata 的新增回归证据，也不宣称 `engine_next` 全量测试通过。
- 本轮为控制面 v2 状态目录补做并验证了恢复 checkpoint：schema `3`、last event `161`，DB backup SHA-256 `fc410973aad787534f4d278ac04f3f47bc34e475ae45a4758b4b4e063466cfc7`，event journal SHA-256 `767e407e1df0f77fcde7cdb49e3173d69eba230592176ca40288c47a54b0b2b`；随后 `doctor`、SQLite `integrity-check` 和 `checkpoint-verify` 均通过。
- 随后执行控制面全量 trusted-invariant 检查：10 个 M2 task 均 `ok=true`、`violations=[]`；并行触发两个 scheduler projection 时一个实例被 `SCHEDULER_ALREADY_RUNNING` 拒绝，单写者 OS lock 仍生效。writer task 的下一动作仍是“使用新 run identity 的 Cobra read-only shadow”，但当前 release 未部署 candidate，不能生成伪造 shadow evidence。
- M2 热板缓存已补一条 Cobra 只读证据：`cache:hot_plates:2026-09-10` 为 hash，`HLEN/HSCAN=50`，50 行 JSON、日期全匹配、source=`kaipan`，canonical payload SHA-256 为 `b9f84a419a3d97acc36b3ea0907bfff66bcccfbb80ba980d85f0dffcc58352bc`；meta 为 `postmarket/today_mode=true`，更新时间 `2026-09-10 17:40:06`。结构与日期 PASS，但 meta 仍缺 `schema_version/available_at_ms/field_units`，故 hot-plates core acceptance 继续 BLOCKED，详见 `docs/evidence/m2_hot_plates_real_20260911.md`。
- hot-plates core runner 已在 Cobra formal runtime 的共享 venv 中执行：validation 副本只读读取真实 hash，core 输出 `UNAVAILABLE`、`actual_trade_date=2026-09-10`、`available_at_ms=null`、`missing_fields=["available_at_unknown"]`，content hash `31d3ea73c4fb22c4ff671fb4a08302c478dc98d4dd40713ebbfb89ed26459137`；runner artifact `/home/exedev/validation/cc-m2-hot-plates-20260911/hot-plates-result.json` SHA-256 为 `121d5e98d58036856ff05aafd06764207d4d04ec4fdca64f2825c1374fd4b336`。这闭环了真实 consumer fail-closed 行为，但不构成 acceptance；只同步 validation 目录，未改 Cobra current/release 或 Redis。
