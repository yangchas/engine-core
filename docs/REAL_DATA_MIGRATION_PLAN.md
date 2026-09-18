# engine_core 真实数据与生命周期迁移计划

### 2026-09-19 M1 下一交易日运行单固化

- 将 `docs/runbooks/m1_live_morning_next_session_20260921.md` 固定到当前已在
  Cobra-ion Python 3.12.3 完整验证的 Core 副本 `cf7edb3`，归档 SHA-256 为
  `ba380df3080256f09aaa9820d353262eadeaac71d98aa01e7025576601d8876d`。
- 下一个交易日只执行真实、只读的启动自检、节点前 reference prefetch、
  `AUCTION_0926` 与 `OPENING_0932`；生产 owner、Rabbit/ACK、Redis/TD writer、
  通知/effect 保持不变。该运行单不替代 t1-v2 的 09:25 source finalization，
  其 09:25 business anchor/09:25:06 settling barrier 合同仍按本计划前条执行。
- `OPENING_0932` 的 Timer/business anchor 仍为 `09:32:00`，但当前 live morning
  shadow 的正式评估 admission 最早为 `09:32:10`；`09:32:00` 仅表示节点到期，
  不表示 opening source cohort 已稳定，source observation time 仍原样保存。

### 2026-09-19 09:25 settling barrier contract correction

- 固定生产时间语义：`09:25:00` 仍是业务锚点和 Timer scheduled time，但该时刻
  行情 cohort 尚未全部补齐；NORMAL `0925` source finalization/evaluation 最早
  为 `09:25:06`（六秒 settling barrier）。源记录时间必须原样保留，不能改写成
  `09:25:00`。
- `run_m3_auction_followup_shadow.py` 在 NORMAL `0925` 未到 `09:25:06` 时
  fail-closed，不读源、不 dispatch Engine；`run_continuous_session_shadow.py`
  对 NORMAL 连续会话同样拒绝早于 barrier 的显式 evaluation time。RECOVERY 或
  POSTMARKET_DIAGNOSTIC 不借此伪造正常交易时段证据。
- 新增合同测试覆盖 `09:25:05` 拒绝与 `09:25:06` 可接受；Timer 合同本身仍只
  表达业务 scheduled time，不把 producer-specific settling gate 混入通用 Timer。

### 2026-09-19 Redis 投影接入单 Engine 连续 Shadow

- `3889df4` 将连续会话入口扩展为两条等价输入路径：已读取的 TD
  `auction_snapshot_v2` 行，或已读取的 Redis `market:auction:{date}:{tag}`
  projection；两者都进入同一 `_run_projection_session`，不创建第二个
  Engine，不在节点重读 Redis，不做缺失槽位修复。
- 新增边界：重复 tag 直接拒绝；Redis `0924` 缺失时保留 projection
  `MISSING`，0924 结果保持等待，0925 结果保持 `MISSING/PARTIAL`，不以
  0920/0925 伪造相邻段。
- Local/Cobra-ion Python 3.12.3 同一归档均为 `576 passed`、compileall
  PASS；新增文件 SHA-256：
  `run_continuous_session_shadow.py=50481eb0df96edcac08ae232bb864d4d00832da6e99a01ed0fdef65835c0bae6`，
  `test_continuous_session_shadow.py=7cf7d0689871e6b5718413eae05ea4aba878a8b403dac0af9115348d53dc7900`。
- 真实 Cobra-ion 盘后只读运行（trade date=`2026-09-18`, symbol=`000338`）
  读取 Redis 0920/0924/0925 均为 `READY`，Q2 为 `5224/5224`、coverage
  `1.0` 但 `STALE`，单 Engine 消费 8 个 signal、输出 4 个 `FACT_ONLY` 结果，
  opening=`PARTIAL`；artifact SHA-256 为
  `d317dbe60ce5a0019c8007bc8211775eb4c5cf9f5bf7e60cf65b45c35776bedc`。
  生产服务仍保持 active；未写 Redis/TD、未接管 Rabbit/ACK、未发送通知或
  effect。该证据只关闭 Redis projection → continuous Core 的窄 seam，
  不关闭正常交易时段、全市场、启动恢复或 engine-next 替代验收。

### 2026-09-18 22:40 单 Engine 连续会话真实 Shadow

- 新增 `examples/run_continuous_session_shadow.py`，将已读取的真实 TD
  `auction_snapshot_v2` 三锚点与真实 Redis Q2 开盘投影，在同一个
  `DeterministicEngine`、同一个 reducer/window 会话中连续消费：
  `AUCTION_0920 → AUCTION_0924 → AUCTION_0925 → OPENING_0932`。
- Cobra-ion Python 3.12.3 对实现 commit `498599f` 重跑完整套件：
  `574 passed`、compileall PASS；本地同样 `574 passed`。随后仅补充本条
  迁移证据文档形成最终 HEAD；新增 runner/test 文件 hash 与远端一致。
- 真实盘后诊断（symbol=`000338`）消费 8 个 signal、产生 4 个结果、无
  pending evaluation；竞价与开盘均保持 `FACT_ONLY`，Q2 为
  `5224/5224`、coverage=`1.0` 但在 10 秒策略下为 `STALE`。Redis/TD
  写入、Rabbit consumer/ACK、通知和 effect 均为 0。
- 该证据只关闭“跨节点共用一个 Engine”的组合 seam，不证明正常交易时段
  09:20–09:32、全市场覆盖、Rabbit batch/source-freeze、启动补数、持久化恢复、
  报告投递或 `engine-next` 替代。artifact 保存在 Cobra-ion 隔离目录，SHA-256
  为 `f1f2a60fc259ba0f2f74510b591f8af5ced1b5a235402969fffefcd500ad1ac4`。

### 2026-09-18 M3 09:24/09:25 后续节点窄接入（代码验证）

- 新增 `examples/run_m3_auction_followup_shadow.py`，只把已经读到的
  `0924`/`0925` Redis projection 接入现有 `SessionRuntimeCoordinator` 和一个
  in-memory Core Engine；不新增调度器、不读取 Q2、不写 Redis/TD、不回填缺失的
  前置竞价槽位。
- `NORMAL` 只允许在对应业务窗口内运行：`0924=[09:24,09:25)`、
  `0925=[09:25,09:26)`；窗口外不创建 Redis 读取或 Engine dispatch。恢复运行
  只能显式使用 `RECOVERY_CATCHUP`，且所有当前及前置 projection 必须在业务锚点
  cutoff 前已经被观察到，否则 fail-closed。
- `0924` 必须由已观察的 `0920` projection 先验提供，`0925` 必须同时提供
  `0920` 与 `0924`；projection tag、交易日、观察时间均逐项校验。当前代码只
  验证节点接入和事实输入边界，AuctionFactShadow 仍由已有三锚点 shadow 入口负责。
- Local 新增 follow-up 合同测试后为 `532 passed`；该项尚未在交易时段执行真实
  NORMAL 采集，不能宣称 09:24/09:25 生产节点已通过，`engine-next` 仍是生产 owner。
- 隔离 Cobra-ion 验证与盘后真实 Redis recovery fail-closed 证据见
  `docs/evidence/m3_followup_node_validation_20260918.md`。

### 2026-09-18 M3 09:20 NORMAL 时间窗口防伪装修复

- 修复 `run_m3_0920_shadow` 的运行单边界：盘后调用不能继续标记
  `origin=NORMAL`，超过 `09:21:00` 直接返回 `BLOCKED`，不读 Q2、不创建
  Engine；晚启动仍必须使用 `RECOVERY_CATCHUP`。
- 新增对应反例单测，验证 fail-closed 且 source read count 为零。Local
  `526 passed`、compileall PASS；Cobra-ion 独立验证副本同样 `526 passed`、
  compileall PASS。生产 `engine-next`/`t1-v2-live` 未改动。
- 证据：`docs/evidence/m3_normal_window_guard_20260918.md`。

### 2026-09-18 19:38 engine-next loader 与 Core 真实竞价投影交叉复核

- 使用正式 `engine-next` release `20260903_e272842` 的既有
  `IntradayDataHub.load_auction_snapshots()`，在 Redis 写保护下只读检查
  `2026-09-18` 的三个竞价槽位：`0920=200`、`0924=200`、`0925=200`。
  因此早先某次采集中的 `0924` 缺失不能泛化为今天当前投影的状态；历史
  capture 仍按当时的证据保存，不回写。
- 对 `000338` 通过 Core 公开 Engine 队列重复运行两次，`6` 个 signal、`3`
  个事实结果，direct/engine `fact_content_hash` 均为
  `e1904a88aa98f5503865054b7388fcc56d14d1427e94c583308c1cee85496c7e`，
  业务事实一致；状态保持 `PARTIAL/FACT_ONLY`，未推导交易结论。
- 当前交叉证据只证明 Redis loader seam 与 Core 组合一致，不证明全市场
  authority、Rabbit batch 归属、上游 AuctionState 同源，或可进行历史 cutoff
  replay。Core 仍未取得生产替代资格。
- 证据：`docs/evidence/real_auction_loader_core_shadow_20260918_1938.md`。

### 2026-09-18 19:42 真实 TD/Redis reference readiness 复核

- 通过现有有界只读 runner 读取 `previous_day_stats`、昨日涨停池、热板和
  Q2：TD daily-kline 返回 `2` 行，Redis reference cache 返回昨日涨停池 `47`
  行、热板 `50` 行，Q2 coverage=`1.0` 但 `5224/5224` stale。
- 因当前查询发生在盘后且没有历史 `available_at_ms` 证明，三个 reference
  结果均保持 `UNAVAILABLE`；整体 readiness=`PARTIAL`，并给出
  `REFRESH_Q2/PREFETCH:*` 动作。这个结果证明 TemporalDataGuard 没有把
  “现在能读到”误判成“09:20 已知”，也不证明下一交易日盘前预取已完成。
- 下一步只需在真实节点前捕获一次 prefetch，并把冻结结果绑定到对应
  Engine evaluation；不新增 Provider/Replay/工作流框架。
- 证据：`docs/evidence/real_reference_readiness_20260918_1942.md`。

### 2026-09-18 19:45 真实 Redis 竞价投影 → Core Engine 复核

- 直接通过 Core `RedisAuctionProjection` adapter 读取生产
  `market:auction:20260918:{0920,0924,0925}`，将 `000338` 的三个槽位提交
  到同一个 in-memory Engine；两次重复运行均为 `6` 个 signal、`3` 个结果，
  `fact_content_hash=bd7660727cbe9967248b99c2ff1ad4bc502d0c5c9a044e55d1efd14379d1582a`。
- 事实保持 `PARTIAL/FACT_ONLY/OBSERVE`：只确认成交额变化和买方剩余量变化，
  未把 Top-Amount 中缺失的价格、卖方盘口、宽度、主题补成零或其它来源。
- 这关闭了当前真实 Redis projection → Core 的确定性消费基线，但不转移
  09:20/09:24/09:25 source freeze ownership，也不构成全市场或生产替代验收。
- 证据：`docs/evidence/real_redis_auction_core_shadow_20260918_1945.md`。

### 2026-09-18 19:36 盘后真实 Q2 / 生产状态复核

- Cobra-ion server time was `2026-09-18 19:36:15 CST`; this is outside the
  market window. `engine-next` and `t1-v2-live` remain `active` with no
  recorded restarts, but `t1-v2 progress` has not advanced since the earlier
  observed afternoon line. Active systemd state is therefore not treated as
  a live-heartbeat proof.
- The real Redis Q2 path read `5224/5224` symbols using the existing
  `SMEMBERS q2:active:20260918` + `HGETALL q2:{symbol}` dialect. Coverage was
  `1.0`, but all `5224` rows were `STALE` under the explicit 10-second policy;
  newest source lag was about `16610s`, with source range
  `1789660800000..1789714805000`.
- Two Core observations over the same real input produced identical probe and
  snapshot hashes. This keeps deterministic shadow verification `PASS`, but
  does not close freshness or production-replacement gates.
- Root disk remains about `93%` used with `1.4G` available; earlier
  `t1-v2` logs contain TD `No enough disk space` commit errors. No cleanup,
  restart, Redis/TD write, Rabbit consumer/ACK change, or effect was made.
- Evidence: `docs/evidence/real_q2_probe_20260918_1936.md`.

### 2026-09-18 19:35 Cobra-ion 功能基线复核

- 在 `/home/exedev/validation/engine-core-6b4f726-v1` 使用正式 Python 3.12.3 共享环境重新执行 `pytest -q -p no:cacheprovider` 与 `compileall`：`525 passed`、compileall `PASS`。
- 当前 `6b4f726` 之后的本地提交仅为 README/证据/运行单文档变更，未改变功能代码；因此该结果可作为当前 Core 功能基线验证，但不证明实时 Q2 freshness、TD 写入健康或替代 `engine-next`。详见 `docs/evidence/cobra_exact_verification_20260918_1935.md`。

### 2026-09-18 19:28–19:30 真实 Q2 停滞复核

- 按真实 Redis 方言（`SMEMBERS q2:active:20260918` + `HGETALL q2:{symbol}`）重新读取 `5224/5224`，coverage=`1.0`，但 `5224/5224` 全部 `STALE`；最新 source lag 约 `16234s`，source range=`1789660800000..1789714805000`。
- 同一次只读运行内两次 Core observation 的 probe/snapshot hash 一致，说明确定性消费仍正常；源时间没有继续推进，与 t1-v2 最后 progress 停在 15:43 的现象相互印证。artifact SHA-256=`a6d39ee964d2cfcaff9f90b3dfca1432e9b0c9b37b614b9ae378d67c84918d10`，详见 `docs/evidence/real_q2_probe_20260918_1928.md`。
- 结论：Redis 读取路径可继续 Shadow，但实时数据源停滞/滞后仍阻止 Core live admission 和替代 `engine-next`。

### 2026-09-18 19:28 Redis Q2 key 方言复核

- 真实 Redis 只读核验确认 `q2:active:{trade_date}` 是包含 symbol 的 `SET`，`q2:{symbol}` 才是逐股 `HASH`；`2026-09-18` active set 有 `5224` 个成员，`000338/600519` 的逐股 hash 均可读。
- 对 active key 使用 `HGET` 会触发 `WRONGTYPE`，不属于生产数据损坏；Core 现有 `RedisQ2ProjectionAdapter` 已按 `SMEMBERS + HGETALL q2:{symbol}` 读取。该证据只关闭 key dialect，不改变 stale/TD/freshness 结论。详见 `docs/evidence/q2_redis_key_shape_20260918_1928.md`。

### 2026-09-18 M1 启动替代差距审计

- 对照部署中的 `engine-next` 启动协调器、bootstrap/self-check 与 Core 当前 `TradingCalendarSnapshot`、`StartupReadiness`、`SessionRuntimeCoordinator`。Core 已关闭纯日历/Q2/计时器/参考数据门禁的 shadow 组合，但仍不拥有日线/因子/筹码/DDE 缺口修复、昨日涨停/热板/板块映射补齐、竞价 anchor recovery、持久化启动状态或正式投递。
- 下一最小迁移单元不是新 Replay/Provider 框架，而是复用已验证访问路径的“单目标日、小股票集、只读启动组合”；它只能预取、冻结、判断和 dispatch，不能 repair、写 Redis/TD、消费 Rabbit 或触发 effect。
- TD `No enough disk space` 仍使 TD parity/替代验收不可用。详见 `docs/evidence/m1_startup_replacement_gap_audit_20260918.md`。

### 2026-09-18 19:20 生产存储压力阻塞 TD 验证

- Cobra-ion 只读检查确认 `engine-next` 与 `t1-v2-live` 均 `active`、`NRestarts=0`，但根盘使用率为 `93%`（`19G` 总量、约 `1.4G` 可用），TD `/var/lib/taos` 约 `5.9G`。
- `t1-v2-live` 在 `14:26:10`–`15:40:53` 期间多次记录 `stage=commit.tdengine | error=No enough disk space`。因此“服务 active”不能作为 TD 写入健康或数据完整的证据。
- 本次没有删除数据、清理 Docker volume、重启服务、修改 Rabbit ACK/consumer、写 Redis/TD 或触发 effect。未有明确 retention/备份审批前，不执行破坏性清理。
- 进一步只读查询确认 `market_data1` 为 `135679` 张表、`keep=3650d,3650d,3650d`；主机 `/var/log/taos` 的 taoslog 文件合计约 `760MB`。这两个都是容量处置候选，但尚未批准 retention 变更或日志轮转，因此本次仍不删除、不压缩、不重启。
- 当前允许继续：真实 Redis 只读 Core shadow，并显式保留 `STALE/PARTIAL`；当前禁止：把 TD 当完整 ground truth、TD 依赖 replay/cross-source parity、Core 替代 owner 验收。详见 `docs/evidence/runtime_storage_disk_pressure_20260918_1920.md`。

### 2026-09-18 19:30 真实第三方参考源连接探查

- 复用旧 `engine-next` release `e272842c8f490f55a1b017badb71e71904ce008e` 的已验证 connector 路径，在 Cobra-ion 共享 Python 3.12.3 环境执行有界只读探查，6/6 连接调用成功；未写 Redis/TD、未消费 Rabbit、未修复缓存、未发通知。
- 仅 BaoStock 日线同时满足请求日期与返回日期闭合（`2026-09-18`），标记 `PASS`。开盘啦 ban reason/热板、同花顺热度、问财涨停结果均保留 `OBSERVED`，因为响应不提供足够的结构化历史日期/可用时间；开盘啦昨日涨停池本次为空，标记 `MISSING`。
- 这些结果只能作为连接性与事后 oracle 证据，不能直接进入历史 Replay 或策略 Bundle；仍需通过 `DataFunction + TemporalDataGuard`。证据见 `docs/evidence/real_reference_probe_20260918_1930.md`，artifact SHA-256=`b3506114b96f260bb1523b2e095471fbe37fb66b75cf835a4d24cc36d32c4870`。

### 2026-09-18 19:20 真实 Q2 复核

- Cobra-ion 现有 Redis Q2 只读探针返回 `5224/5224`、coverage=`1.0`、missing=`0`，但在显式 `10s` freshness policy 下 `5224/5224` 均为 `STALE`，最新 source lag 约 `14893s`。coverage 不升级为 READY，Core 继续 fail-closed。
- 同一真实 observation 两次进入 Core 后 probe/snapshot hash 完全一致；该结果证明真实连接和确定性消费，不证明盘中 freshness、Rabbit arrival 或 Core 替代 `engine-next`。
- 证据：`docs/evidence/real_q2_probe_20260918_1920.md`，远端 artifact SHA-256=`14847cef2d09d773f6214edbf563c5faf86ad350a4f05614a96077e467a95466`。

### 2026-09-18 19:01–19:03 盘后真实 Redis → Core Shadow 复核

- 使用 Cobra-ion 既有 engine-next Python 3.12.3 共享虚拟环境中的 `redis 8.1.0`，对真实 `market:auction:20260918:{0920,0924,0925}` 执行只读 HGETALL，并将结果送入 Core 的公开 Engine signal path。未新增 Rabbit consumer、未改变 ACK、未写 Redis/TD、未触发通知/effect，`engine-next` 与 `t1-v2-live` 仍为生产 owner。
- `0925` 投影在 `TOP_AMOUNT` 范围内为 `READY`，但 `600519` 不在该 200 行范围，Core 正确返回 `MISSING`；没有用零值或其它来源补齐。选取实际存在的 `000338` 后，Core 返回 `PARTIAL/FACT_ONLY`，`amount_delta_yuan=24231638`，缺失价格/盘口字段保持未知。
- 同一真实输入重复执行时，`fact_content_hash` 与 `0920/0924/0925` projection hash 一致；证据 hash 随观测上下文变化属于预期。该结果证明真实 Redis seam 可执行和可重复，但不证明 TopN 是全市场 authority，也不构成正常开盘验收或 engine-next 替代。
- 证据：`docs/evidence/real_redis_shadow_20260918_1901.md`；远端 artifact 为 `/home/exedev/validation/engine-core-shadow-20260918-1901-600519.json`、`/home/exedev/validation/engine-core-shadow-20260918-1902-000338.json` 和重复运行文件。

### 2026-09-18 周一 M3-1 日历 guard 收口

- `m3_0920_next_session_20260921.md` 增加目标交易日存在性检查；Cobra-ion 复核 `cc-m0-calendar-20260911-v3.json` 的 guard 日期包含 `2026-09-21`，声明覆盖至 `2026-12-31`。目标日期缺失时运行单直接 `BLOCKED`，不进入 recovery/fallback。
- 该修正只改变运行前 fail-closed 证据，不改变生产服务、日历内容或 Core 计算；提交为 `bf6b43e`。

### 2026-09-18 M3-1 09:20 preflight shadow

- Added only the bounded M3-1 composition: one real Redis Q2 prefetch,
  calendar/session self-check, `SessionRuntimeCoordinator` timer identity,
  cutoff validation and one existing Engine instance. Preflight failure is
  fail-closed; no fallback, repair, persistence or effect was added.
- Local and Cobra-ion isolated suites both pass `525` with compileall. The
  real post-market Cobra run for `000338` was intentionally
  `RECOVERY_CATCHUP` and returned `BLOCKED` because current Q2 observation was
  after the 09:20 anchor; Engine dispatch stayed false. Artifact SHA-256:
  `a6d0ab5d5948c664e7d0148326b848b1386780d42c097b18c4e5003fc207e570`.
- This closes only M3-1's code and fail-closed recovery boundary. A normal
  09:20 run before/at the node is still required; 09:24 and 09:25 remain
  separate slices and `engine-next` remains the production owner.

### 2026-09-18 M2 Redis projection -> Core Engine queue

- Added a bounded read-only runner that consumes the existing Redis auction
  projection adapter through the public `DeterministicEngine` queue. It submits
  only three market-update/timer pairs for 0920/0924/0925 and emits fact-only
  shadow output; no provider framework, production writer, Rabbit action or
  effect was added.
- A real Cobra-ion run for TopN symbol `000338` processed 6 signals and 3
  strategy results, preserved source times, and returned `PARTIAL/FACT_ONLY`.
  The Redis projection remains TopN-scoped and does not promote `price_yuan`
  into an unverified `price_milli`; missing price/ask/pressure stays missing.
- Local and isolated Cobra-ion suites both pass `521` with compileall. Latest
  real artifact SHA-256 is
  `686dff98811a9f0f3e207e31883fad2f31765201323c9fb646f73dfeb3efce28`.
  This is M2 bounded seam evidence only, not engine-next replacement.

### 2026-09-18 M2 Redis auction projection adapter

- A thin Core read-only adapter now extracts the exact legacy Redis snapshot
  contract (`summary`/`top_amount` from `market:auction:{date}:{tag}`) without
  importing `engine-next` or carrying its recovery/write paths. The adapter
  preserves `TOP_AMOUNT` scope, missing tags, missing fields and stable
  logical evidence references; it never turns TopN into a full-market fact.
- Cobra-ion real Redis verification returned 200 rows for each of 0920/0924/0925
  on 2026-09-18. The requested bounded symbols were absent from all TopN rows
  and remained missing. Local/Cobra suites both pass `517`; no Redis writes or
  production changes occurred. Differential evidence is in
  `docs/evidence/m2_redis_projection_adapter_20260918.md`.
- Legacy/Core shared amount and bid values match. Legacy zero-fills absent ask,
  price and change fields; Core intentionally preserves `None`/missing. This
  is an explicit semantic correction, not a claim of byte-for-byte old output
  parity. Full-universe, freeze ownership and replacement remain open.
- The existing Core Redis↔TD comparison seam now calls this adapter for Redis
  snapshot reads. A real common TopN symbol (`000338`) matched amount and bid
  fields at all three tags; missing Redis ask remained `NOT_COMPARABLE`. This
  closes the bounded source-consumption seam, not full Engine auction
  ownership.

### 2026-09-18 M2 legacy/Core differential

- Real current-day probes show the deployed legacy loader returns 200 TopN rows per 0920/0924/0925 but not the bounded symbols, while legacy context reads those symbols and clamps future Q2 age to zero. Core TD auction Engine shadow processes all three symbols with direct/Engine semantic equality and `PARTIAL/FACT_ONLY`. Because authorities/as-of cohorts differ, numeric differences remain `NOT_COMPARABLE`; no strategy migration or producer change follows from this evidence. Evidence: `docs/evidence/m2_legacy_core_differential_20260918.md`.

### 2026-09-18 M1 real recovery shadow

- The isolated Cobra-ion copy ran the bounded morning shell at 17:35 CST with real Redis Q2 and TD reads for `600519`. It emitted startup, `AUCTION_0926`, and `OPENING_0932`; startup checkpoint traces were `RECOVERY_CATCHUP/PARTIAL/STALE`, opening preserved the stale-Q2 gate, and all safety counters were zero. This is post-market recovery evidence only; normal-origin 09:20/09:25/09:32 and replacement acceptance remain open. Evidence: `docs/evidence/m1_live_recovery_shadow_20260918_1735.md`.

### 2026-09-18 M1 real reference shadow

- Real read-only reference probing reached TD `daily_kline` and Redis yesterday-limit/hot-plate sources (`47`/`50` rows, HLEN/HSCAN consistent), but the fixed same-instant cutoff correctly kept all three results `UNAVAILABLE` because `available_at_ms` is unknown and fetch completion was later than the cutoff. Q2 was `STALE` at `5224/5224`. This validates fail-closed timing; a true pre-09:20 prefetch/reuse observation is still required. Artifact SHA-256=`7852f547d22c054d525fa56f10a5df7f32e1b1e04c277dc15857be3088bcd5d0`.

### 2026-09-18 M1 real Redis startup probe

- The isolated read-only startup probe reached the real Cobra-ion Redis Q2 path after market close: `5224/5224`, coverage `1.0`, source quality `STALE/BEST_EFFORT_STALE`, readiness `PARTIAL`, observed at 17:29 CST. This confirms the new trace path uses real Redis data and remains fail-closed on stale input; it is not live opening acceptance. Artifact SHA-256=`a60b45b06488db67f5938285907f8b71eb9d172408a417583ab6b5b13e21dded`. No production restart/write/effect occurred.

### 2026-09-18 M1 startup shadow trace integration

- Commit `b4c2bc4` wires the pure startup checkpoint trace into the existing bounded read-only morning shadow. `startup.json` now records 08:30/09:00 checkpoint identity, business anchor, observation boundary and readiness/Q2 status without adding a scheduler or taking production ownership. Local and Cobra-ion Python 3.12.3 isolated suites both pass `511`; compileall and changed-file SHA-256 equality pass. The next step is isolated real-data execution, not production replacement.

### 2026-09-18 M1 pure startup checkpoint trace

- Commit `b7c1c25` adds `StartupCheckpointTraceV1` for the already-observed 08:30/09:00 readiness boundary. It keeps business anchor time, observation time, Q2 identity and timer-firing identity explicit while remaining side-effect-free: no provider acquisition, repair, persistence, Rabbit consumer/ACK, Redis/TD write, notification, or effect. Local and Cobra-ion Python 3.12.3 isolated suites both pass `511`; compileall and changed-file SHA-256 equality pass. This does not transfer production startup ownership from `engine-next`; the next step is a bounded read-only shadow integration.

### 2026-09-18 M0 startup parity audit

- The deployed legacy startup path still owns 08:30/09:00 checkpoints, formal kline/factor/chip/DDE gap classification, dated reference readiness and bounded repair recommendations. Core `StartupReadiness`/`SessionRuntimeCoordinator` currently evaluate already-observed inputs and dispatch read-only 0926/0932 shadow nodes; they do not acquire, repair, persist, or own those legacy startup actions. 0920/0924/0925 source freeze remains external to t1-v2. This is a replacement-gap audit, not an acceptance claim. Evidence: `docs/evidence/m0_startup_parity_audit_20260918.md`.

### 2026-09-18 Legacy Q2 amount mapping and source-priority boundary

- The real bounded context probe closes only the raw field mapping `Redis Q2 am → Core auction_amount_yuan` for 000001 and 000002. 600519 is retained as a source-priority divergence (`Q2 am=14,312,200` while the legacy context uses auction projection `14,271,787`), so the value is not normalized away and no full Redis-Q2/legacy-projection parity is claimed. The new regression test and frozen extraction are read-only evidence; plate and strategy parity remain open. Evidence: `docs/evidence/legacy_q2_amount_mapping_20260918.md`.

### 2026-09-18 Legacy active-consumer temporal audit

- A bounded read-only probe of the exact deployed `engine-next` context path used real Redis Q2 and executed the legacy plate-bucket fact helper without writes. Under a simulated 09:26 cutoff, the old path observed future Q2 source timestamps and clamped their age to zero. This is recorded as legacy behavior evidence, not a Core contract; Core keeps future-source rejection fail-closed. Plate strings were mojibake and plate/opening labels remain `OBSERVED/UNKNOWN`, so no strategy parity is claimed. Evidence: `docs/evidence/legacy_active_consumer_probe_20260918_1645.md`.

### 2026-09-18 Gate B numeric auction parity

- `417d2d9` closes one capability-local differential slice against the old pure auction shadow helper for the real 600519 `0920→0924` fixture. Core matches price/amount/resting-bid/resting-ask/pressure deltas exactly; legacy directional labels and strategy thresholds remain intentionally unmigrated.
- Local and cobra-ion isolated suites pass `508`; compileall passes. This does not close full auction strategy, plate/locked-order, report delivery, or engine-next replacement parity. Evidence: `docs/evidence/gate_b_legacy_numeric_parity_20260918.md`.

### 2026-09-18 Legacy report status parity closure

- `bcb7f68`/`f521a47` closed the capability-local report status boundary: every Core `FactStatus` maps deterministically to the verified legacy three-state presentation contract (`COMPLETE`, `PARTIAL`, `DATA_UNAVAILABLE`). Local and cobra-ion isolated suites both pass `507`; compileall passes; the bounded real TD auction report shadow for `600519` remains deterministic and direct/Engine semantic hashes remain equal.
- This is not full legacy report parity. Plate rows, locked-order tables, mapping, lifecycle claim/dedupe, delivery, and strategy-console output remain outside Core; `engine-next` remains the production owner.
- No production service, Redis/TD writer, Rabbit consumer/ACK, notification, or effect path changed. Evidence: `docs/evidence/legacy_report_status_parity_20260918.md`.

### 2026-09-18 昨日涨停结构报告投影

- `6a56388` 将 `PreviousDayLimitStructureFact` 作为可选事实段接入 build-only 报告；只输出上一交易日结构，不把未知的当日反馈、板块/封单或策略结论写进报告。Local/Cobra 均 `487 passed`、compileall PASS，旧 `engine-next` 仍是正式报告/投递 owner。详见 `docs/evidence/previous_limit_report_projection_20260918.md`。

### 2026-09-18 昨日涨停结构事实切片

- `4ca5915`/`bfbfd85` 新增并加固纯 `PreviousDayLimitStructureFact`，只从已守门的昨日涨停池派生数量、最高板和板高分布；不计算当日反馈、不做板块/策略判断，板高映射在 hash 后保持不可变。真实 Cobra-ion Redis capture 的 47 行数据被纳入冻结 fixture，HLEN/scan 一致且解码错误为 0。
- 因真实 metadata 没有可证明历史 `available_at_ms`，`PreviousDayLimitPoolFunction` 与结构事实均保持 `UNAVAILABLE`，未把真实 rows 偷渡成 runtime-ready；真实 runner 已输出相同 fail-closed 结果。Local/Cobra 均 `488 passed`、compileall PASS；详见 `docs/evidence/previous_day_limit_structure_fact_20260918.md`。

### 2026-09-18 A2 report evidence-lineage correction

- `ec5dd37` 修复 A2 报告切片的两个合同缺口：summary mapping 补齐 `limit_up_seal_amount_yuan`；报告 `evidence_hash` 纳入 summary evidence identity，而 `semantic_hash` 仍只表达业务语义。未改变 Provider、Engine、策略、投递或生产链路。
- 本地 `482 passed`、compileall PASS；新增回归测试证明相同业务结果但不同 summary evidence 时 semantic hash 相同、evidence hash 不同。详见 `docs/evidence/auction_report_lineage_fix_20260918.md`。

### 2026-09-18 A2 summary report projection integration

- `adaf831` 将已归一化的 `AuctionMarketSummaryFact` 作为可选输入接入 build-only `AuctionFactReportArtifact`。报告只组合冻结事实，不读取 Redis/TD、不恢复、不 claim、不发邮件/通知、不做策略判断；summary 的语义 hash 与 evidence/provenance 分离。
- 本地与 Cobra-ion 隔离副本均 `481 passed`、compileall PASS。该项只关闭 A2 summary 的报告投影，不代表完整旧报告 parity，也不改变 `engine-next` 的生产 report/effect owner。详见 `docs/evidence/auction_report_a2_integration_20260918.md`。

### 2026-09-18 A2 市场汇总事实轮子

- 从真实 `auction_0920` capture 的 Redis `summary` 字段提取受控 Golden fixture，新增 `AuctionMarketSummaryFact` 与 `normalize_auction_market_summary()`。字段只在边界接受旧 raw alias，内部使用明确 count/yuan 单位；缺失、非法、显式零分别处理，不推断上游时间语义。
- 本地/Cobra-ion 均 `480 passed`、compileall PASS；无生产写入或服务变更。该项只关闭 A2 summary normalization，不宣称完整报告、板块聚合或实时 freshness parity。详见 `docs/evidence/auction_market_summary_fact_20260918.md`。

### 2026-09-18 build-only 报告投影

- Core 新增最小 `AuctionFactReportArtifact`，仅将已冻结 `AuctionFactShadow` 投影为结构化/文本事实产物，保留数据来源、状态、semantic/evidence hash 和 provenance；不接管通知、claim、SMTP、Webhook、恢复或策略结论。提交 `69f8535`，本地/Cobra 隔离套件均 `474 passed`、compileall PASS。旧报告字段 parity 仍按能力逐项推进，不能把该单股 fact 产物宣称为完整邮件等价物。详见 `docs/evidence/build_only_report_projection_20260918.md`。
- `engine-next` 继续作为生产报告/副作用 owner；该提交不改变生产服务和真实数据结论。

### 2026-09-18 14:47 盘中真实 Q2 复验

- Cobra-ion 只读 Redis Q2 返回 `5224/5224`、coverage=`1.0`，但全量 `STALE/BEST_EFFORT_STALE`，最新 source lag 约 `1811s`；重复 Core observation hash 一致。artifact SHA-256=`9f98da4eb83d4c1965a37eb6d062d5b97b1dc786558815b10c35d67eb8e758d9`，详见 `docs/evidence/real_live_q2_20260918_1447.md`。
- 该结果证明真实 Q2 只读路径可用，不证明实时新鲜度或 Core 可替代 `engine-next`；t1-v2 仍报告 TD 磁盘不足，继续保留生产主链与只读 Shadow 边界。

### 2026-09-18 14:47 Legacy reporting boundary audit

- 只读审计旧 `engine-next@20260903_e272842` 的 fact assembly、build-only report、delivery lifecycle/claim、notifier 和 strategy-console 边界，确认 Core 后续只能先承接冻结事实上的 build-only projection，不能接管 Redis claim、SMTP/Webhook、恢复补数、通知或策略阈值。证据见 `docs/evidence/legacy_reporting_contract_audit_20260918.md`。
- 当前 `ENGINE_CORE_REPORT_OWNER=NOT_READY`。生产 `engine-next`/`t1-v2-live` 继续作为 owner；盘中 TD `No enough disk space` 与 Q2 stale 门禁仍阻止 live replacement，离线报告合同审计可以继续。

### 2026-09-18 14:34–14:38 真实上游滞后与存储容量告警

- `t1-v2-live` 仍 active、持续消费并 ACK，但日志多次报告 `commit.tdengine: No enough disk space`；wall lag 约 26–27 分钟。Core 新的真实 Q2 只读观测仍为 `5224/5224`、coverage=`1.0`，但全量 stale，最新 source lag 约 `1615s`，未放宽 freshness gate。
- 根盘约 `94%` 使用率、剩余 `1.2G`；活动 `infra_tdengine-data` 约 `6.1G`，`market_data1` 保留策略为 `3650d`。本次未删除生产数据、未 prune Docker volume、未重启服务、未改变 Rabbit/ACK。
- 该证据将当前阻塞明确为“生产上游 TD 容量/积压问题”，不是 Core 计算问题。下一步需要单独审批精确的 retention/容量处理后，才能重新验证 fresh Q2/TD；Core 继续只读 Shadow。详见 `docs/evidence/runtime_storage_lag_20260918_1438.md`。

### 2026-09-18 14:28 盘中真实预取 cutoff 复验

- 在 Cobra-ion 生产环境旁路隔离副本执行新的只读观测：真实 Redis Q2 为 `5224/5224`、coverage=`1.0`，但全量 `STALE/BEST_EFFORT_STALE`，最新 source time 为 `1789711401000` ms；重复 Core 观测 hash 一致。
- 使用显式的一分钟未来节点 cutoff 模拟“节点前预取”：TD `previous_day_stats=READY`，Redis `previous_day_limit_pool=READY`（47 行），`hot_plates=UNAVAILABLE`（`hot/strength/net_inflow_yi` 单位仍未闭环）；`temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`、整体 readiness=`PARTIAL`。
- 这只证明 LIVE 预取合同可以在明确 cutoff 下放行，不把 `observed_at` 冒充历史 `available_at`，也不改变 Q2 stale gate。生产 `engine-next`/`t1-v2-live` 保持 active，未新增 Rabbit consumer/ACK、未写 Redis/TD、未触发 effect。证据见 `docs/evidence/real_live_reference_readiness_20260918_1429.md`。

### 2026-09-18 真实 Linux 复验：canonical unit guard

- 为 `turnover_yuan` 增加固定单位校验，拒绝调用方/metadata 将 canonical yuan 字段覆盖成 percent 等其他单位；本地与 Cobra-ion 全套分别为 `469 passed`、`compileall PASS`。
- 同一真实 Redis/TD 只读旁路复验结果保持不变：`previous_day_stats=READY`、`previous_day_limit_pool=READY`（47 行）、`hot_plates=UNAVAILABLE`、Q2 `5224/5224 STALE`、整体 readiness=`PARTIAL`。Follow-up artifact SHA-256=`2957af09e890edda895f575e39b3f019fd6a3317edf30fef94f8a3f807a8e8de`。

### 2026-09-18 真实 LIVE turnover canonicalization 复核

- 将旧 Redis/Kaipan raw `turnover` 在 Core Provider 边界映射为 `turnover_yuan` 后，在 Cobra-ion 隔离副本使用真实 Redis/TD 重新运行：`previous_day_stats=READY`、`previous_day_limit_pool=READY`（47 行）、`hot_plates=UNAVAILABLE`（strength/hot/net_inflow_yi 单位仍未闭环），Q2 `5224/5224` 但仍为 `STALE`，整体 readiness=`PARTIAL`，`temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`。
- 远端 artifact `/home/exedev/validation/engine-core-6511981-v1/reference_readiness_live_turnover_20260918.json` SHA-256=`6ed6e2f5fe2b967367dd9b033d8e94f489f3e4c2ff35b29b126153355a19735b`；同一隔离副本 `468 passed`、`compileall PASS`。生产 `engine-next`/`t1-v2-live` 保持 active、`NRestarts=0`，无写入、无 Rabbit consumer/ACK、无 effect。详见 `docs/evidence/real_live_reference_turnover_20260918.md`。

### 2026-09-18 继续审计：Legacy reference semantics 收口

- 只读核对 Cobra-ion `engine-next@20260903_e272842` 的 `StockAnalyzer.get_history_bans_pool()` 与 `KaipanConnector`：`rec[9]` 是旧 payload 的金额型 `turnover`，`rec[2]` 是百分比点 `close_pct`，而不是旧 schema 中错误标注的百分比 turnover。真实 Redis `cache:yest_limit_pool:2026-09-17` 的 47 行样本（约 6,181 万～15.2 亿）与该语义一致。
- Core Provider 边界现将 legacy raw `turnover` 映射为明确的 canonical `turnover_yuan`；不做数值换算，也不向事实/策略层暴露无单位 `turnover`。`close_pct` 保持百分比点。对应测试已改为验证 canonical unit。
- 热点板块旧 connector 的 tuple 路径 `tuple[6]/1e8 -> net_inflow_yi` 仅记录为 source-formula evidence；dict 路径对 `net_inflow`/`net_amount`/`main_net` 不做同样换算，因此当前 `net_inflow_yi` 仍不能全局放行。Redis `strength/hot` 约 89～153，与旧 consumer 的 `strength>=3000` 阈值不在同一已证实量纲，继续保持 UNKNOWN/NOT_AUTHORIZED，不猜测换算、不迁移阈值。
- 真实 Redis metadata 仍缺 `schema_version/field_units/available_at_ms`；本次只关闭 limit-pool 数值单位歧义，不改变 HISTORICAL/REPLAY 的 availability fail-closed 合同。证据：`docs/evidence/legacy_reference_semantics_20260918.md`。

### 2026-09-18 13:49–13:55 LIVE reference readiness 合同收口

- 为真实 Provider 增加显式 `temporal_mode` 和 `fetch_completed_at_ms`：HISTORICAL/REPLAY 仍要求已证明的 `available_at_ms`；LIVE 只在 `fetch_completed_at_ms <=` 明确的节点/预取 `knowledge_as_of_ms` 时放行，绝不把读取完成时间写成历史可用时间。
- `run_live_morning_shadow` 的参考预取现在使用实际时钟记录完成时间，并把 09:26 作为显式节点截止；晚启动或超过截止的读取保持 fail-closed，不再用进程启动瞬间冒充完成时间。
- 第一次真实探针故意使用读取开始时刻作为 cutoff，三项参考数据均因完成时间晚于 cutoff 而 `UNAVAILABLE`；随后用显式的一分钟预取 cutoff 重跑，TD `previous_day_stats` 为 `READY`，Redis 昨日涨停池/热板因真实字段单位未闭环保持 `UNAVAILABLE`。`temporal_live_readiness=PASS`、`temporal_historical_proof=UNAVAILABLE`、整体 readiness=`PARTIAL`。
- Q2 真实返回 `5224/5224`、coverage=`1.0`，但在 60 秒策略下仍为 `STALE`；Redis 昨日涨停池 47 行、热板 50 行，HLEN/HSCAN 一致。全程只读，无 Rabbit consumer/ACK、Redis/TD 写入、修复、回退或 effect。
- 本地与 Cobra-ion 隔离副本均为 `468 passed`、`compileall PASS`。证据：`docs/evidence/real_live_reference_readiness_20260918_1355.md`。这只关闭 LIVE 参考数据时间门禁，不改变历史回放合同，也不表示 Core 已可替代 engine-next。

### 2026-09-18 13:12–13:18 真实 Redis / engine-next / Core 只读链路复核

- `engine-next` 与 `t1-v2-live` 仍为 active、`NRestarts=0`；未新增 Rabbit consumer、未改变 ACK、未写 Redis/TD、未重启生产服务。真实 Redis Q2 返回 `5224/5224`、coverage=`1.0`，但 `stale=5224`，最新源时间落后约 `6691s`，因此状态保持 `STALE/BEST_EFFORT_STALE`，重复 Core hash 一致；coverage 不升级为 READY。
- 使用生产 release `20260903_e272842` 的旧只读路径：auction loader 在 0920/0924/0925 各返回 200 条 Top-200 投影，context probe 读取 3 个 bounded symbol；两条 projection 不共享 immutable as-of，不能宣称 exact parity，`guard_writes=[]`。
- Core public Engine 对真实 Redis Q2 opening 输入和真实 TD `auction_snapshot_v2` auction 输入均完成 bounded shadow。Opening 三只标的均 `projection=STALE`、`fact=PARTIAL`、`state=OBSERVE`；Auction 三只标的均 `direct_fact_hash == engine_fact_hash`，但仍为 `FACT_ONLY/PARTIAL`。这证明真实只读事实链可运行，不构成替代生产 owner。
- 真实日历 probe 使用 BaoStock 登录/query/logout 只读路径生成当前可用范围快照：查询 `2025-12-01..2026-09-18`，声明覆盖 `2026-01-01..2026-09-18`，`197` 个交易日；不能把未来尚未由源发布的日期写入 guard coverage。artifact 位于 Cobra-ion `/home/exedev/validation/calendar-probe-20260918/calendar.json`，文件 SHA-256=`b3e633497be579ab20dd31231af4257d1dcb417a35af1f059295bdb43a47e6f4`，semantic hash=`8f2a56c8dca12d7a37779fb14961ab5fb0bed21d03ef4aaf3a4c7b8d76c3b96f`。
- 一次第三方连接器探针在超出命令预算后被停止，不采纳其结果；这进一步确认第三方网络 I/O 必须有界、不能阻塞 Core reducer。完整证据见 `docs/evidence/real_readonly_chain_20260918_1315.md`。

### 2026-09-18 13:23 真实参考数据 readiness 复核

- 使用当天 BaoStock 日历快照推导唯一 `previous_trade_date=2026-09-17`，真实读取 TD `daily_kline`、Redis `cache:yest_limit_pool:2026-09-17`（47 行）和 Redis `cache:hot_plates:2026-09-18`（50 行）；Redis HLEN/HSCAN、日期、来源和行数一致。
- 三个结果均因没有可证明的历史 `available_at_ms` 保持 `UNAVAILABLE/available_at_unknown`，`observed_at` 没有被冒充可用时间；Q2 同时为 `5224/5224`、coverage=`1.0` 但 `STALE`。readiness=`PARTIAL`，动作仅为 `REFRESH_Q2` 与三个 `PREFETCH`。
- 该结果关闭了真实连接、日期 authority 和 fail-closed 行为，但没有关闭 M2 runtime readiness；下一步必须获得 producer availability/field-unit evidence，或由架构决策明确 live-only policy，不能私自放宽 TemporalDataGuard。详见 `docs/evidence/real_reference_readiness_20260918_1323.md`。

### 2026-09-18 12:27–12:30 真实 Q2/参考数据/Core Shadow 复核

- 在不重启 `engine-next`/`t1-v2-live`、不新增 Rabbit consumer、不改变 ACK、不中断生产链的前提下，使用 Cobra-ion 部署 venv 对真实 Redis/TD 做只读复核。两个服务仍 `active`、`NRestarts=0`；根盘约 85% 使用率、可用约 2.8G，未进行盘中清理或写入。
- Redis Q2 真实观测为 `5224/5224`、coverage=`1.0`，但最新 `source_record_time_ms` 落后约 `6676s`，全量 5224 行均 `STALE`，Core 重复计算 hash 一致。该结果证明读路径和确定性，不构成当前盘中 fresh 资格；不得为了得到 READY 放宽 stale gate。
- 真实 reference readiness 读到 `cache:hot_plates:2026-09-18` 50 行、`cache:yest_limit_pool:2026-09-17` 47 行及 3 条 TD 日线，但三类来源都缺可证明的历史 `available_at_ms`，按合同保持 `UNAVAILABLE`；`observed_at` 未被冒充为 `available_at`。readiness=`PARTIAL/LUNCH_BREAK`。
- 600519 真实 `auction_snapshot_v2` 经过 Core public Engine path，处理 6 个 signal、产生 3 个 fact-only 结果，direct/engine semantic hash 相等，状态 `PARTIAL`；opening shadow 读取真实 Redis Q2 后 coverage=`1.0` 但 projection=`STALE`、5224 行 stale，保持 `PARTIAL`。
- 证据文件：`docs/evidence/real_live_shadow_20260918_1227.md`；远端原始产物保存在 `/home/exedev/validation/engine-core-6511981-v1/`。当前结论：`REAL_REDIS_READ_PATH=PASS`、`REAL_TD_AUCTION_SHADOW=PASS`、`REFERENCE_TIME_SAFETY=PASS`、`LIVE_Q2_FRESHNESS=WARN`，Core 仍不能替代生产 owner。
- 同时完成了真实 Redis 映射方言审计：`market:stock_plate`/`market:stock_reason` 是 plain string hash，`config:plate_mapping:s2p` 是 JSON-list hash；没有日期或 `available_at` 元数据，因此只关闭解码歧义，不把这些 runtime enrichment keys 提升为历史 Replay 输入。详见 `docs/evidence/real_redis_mapping_dialect_20260918.md`。

### 2026-09-18 正常起盘节点 Shadow

- Cobra-ion 只读 Shadow 于 09:13 启动，09:26/09:32 两个节点均以 `NORMAL` 触发，分别仅晚 91ms/10ms；不再是晚启动 `RECOVERY_CATCHUP` 证据。
- 000001/000002/600519 的 TD 0920/0924/0925 行齐全，0924→0925 Auction Fact 和 09:32 Opening Transition Fact 均 READY；Engine 与纯轮子 fact hash 相等，source record time 原样保留。
- Q2 覆盖率为 1.0，但 09:26/09:32 均按真实质量保持 PARTIAL；09:32 为 5224/5224、16 个 stale symbol。启动时 `previous_day_stats/previous_day_limit_pool` 仍 UNAVAILABLE，`hot_plates` MISSING，所以参考数据 readiness 未闭环。
- manifest 记录 Rabbit/ACK、Redis/TD write、notification/effect、production restart 全部为 0。同日未观察到 TD 磁盘不足错误，但 10:06 时 t1-v2 wall lag 已增至约 500 秒，实时新鲜度仍为 WARN。
- 详细时间、事实、SHA 与验收边界见 `docs/evidence/live_shadow_normal_20260918_7e61862.md`。该证据关闭 M1 缺失的 normal-origin timer 空白，但不关闭启动参考数据、全量 legacy differential 或 Core 替代门槛。

### 2026-09-17 engine-next / Core 同输入 Q2 差分

- `EngineNextContextProbeV3` 在不改变旧 Redis pipeline 返回顺序的前提下，只记录 000001/000002/600519 实际被旧 context 链读到的 `stock:quote:*` / `q2:*` Hash；未分类 pipeline 方法改为 fail-closed，`guard_writes=[]`。
- 将同一批冻结 raw Q2 交给 Core `normalize_q2()` + `build_open_fact()` 后，涨跌幅（Core 百分点 vs legacy 比例 x100）、`amount_2m_yuan` 均为 3/3 MATCH；Q2 `speed_1m_bp` 与 legacy ratio 的 `/10000` 变换为 OBSERVED，Core 事实继续保持 `speed_1m=None`，不偷换单位。
- 600519 的当前 Q2 `am=17,611,700` 与旧 context `auction_amount=16,856,932` 不同，证明旧链会使用冻结竞价投影覆盖当前 Q2；这是 source-priority 合同，不是计算 mismatch，Core 不得用当前 `am` 覆盖 0925 reference。
- 随后 Core 独立重读时 000001/000002 的 source timestamp 已前进，因此只保留 ASOF/UNPROVEN，不因数值接近宣称 exact。精确证据、SHA 和验收边界见 `docs/evidence/engine_next_q2_same_input_differential_20260917.md`。

### 2026-09-17 真实生产日只读 morning shadow

- 使用 Cobra-ion 上的 `engine_core` 只读副本 `72e3c17`，真实读取 Redis Q2、TD 竞价行和节点前 reference preparation；`AUCTION_0926` 与 `OPENING_0932` 均完成，三个 bounded symbols 均执行 in-memory Core。预取 reference 通过 `ENGINE_DATA_READY` 绑定，auction direct/engine semantic hash 一致；000001/000002 的 0924→0925 fact 为 READY，600519 因真实字段缺失保持 PARTIAL/UNAVAILABLE。
- 节点实际晚于 09:15 启动，两个 timer 均诚实标记 `RECOVERY_CATCHUP`；Q2 coverage 为 `1.0` 但 freshness/completeness 为 `PARTIAL`，没有把覆盖率升级为 READY。source-record time range 原样保留，未用 TD 行推断 Rabbit batch/arrival 顺序。
- manifest safety 记录 `new_rabbit_consumer=0`、`rabbit_ack_or_publish=0`、`redis_write=0`、`td_write=0`、`notification_or_effect=0`、`production_restart=0`；`engine-next` 与 `t1-v2-live` 完成时仍 active、`NRestarts=0`。本次是真实数据 shadow，不是 Core 替代生产 owner 的验收。
- 远端四个 JSON 证据已复制到 `tmp/live-morning-shadow-20260917-2414eed/`，本地/远端 SHA-256 完全一致；详见 `docs/evidence/live_shadow_20260917_2414eed.md`。当前仍缺正常起盘 `NORMAL` 证据、engine-next loader trace 和 runtime batch membership，联合结论保持 `WARN`。

### 2026-09-15 真实实时旁路复核

- Cobra-ion 11:05–11:08 的只读 Redis Q2/Opening probe 使用真实生产 Q2：5220/5220 行、coverage `1.0`、字段完整，但在 60s freshness policy 下全量 `STALE`，最新 source time 约滞后 75 分钟。两次 Core 计算的 probe/snapshot hash 一致。
- TD `market_data1.stock_tick_v2` 在 11:07 查询到今日数据至 09:51:55；`market_data1.auction_snapshot_v2` 今日无行。`engine-next`/`t1-v2-live` 仍是生产 owner；未写 Redis/TD、未消费 Rabbit、未发送通知或重启服务。
- Opening 单股 fact 的 `status=available` 只表示字段存在且可解析，不能覆盖批量 `projection_status=STALE`/`freshness_status=STALE_OR_MIXED`。本次是真实数据 Shadow 证据，不是 normal-origin 09:26/09:32 证据，也不改变 Core replacement 结论。详见 `docs/evidence/real_live_probe_20260915_1106.md`。

### 2026-09-14 当前离线验证基线（最新）

- 代码/测试提交 `12483d7` 及其后文档头 `fb966c0` 的当前离线套件为 `404 passed`；Cobra-ion Python 3.12.3 对同一归档、`compileall` 均通过。新增日期绑定 Provider 边界测试只强化 `HotPlatesFunction` 与 `PreviousDayLimitPoolFunction` 的 fail-closed 合同，不改变真实数据、生产链或 Core replacement 结论。详见 `docs/evidence/cobra_exact_verification_20260914_12483d7.md`。
- 当前真实生产主链仍由 `engine-next`/`t1-v2-live` 持有；已排定的 2026-09-15 只读 capture/morning shadow 使用独立验证目录，禁止新增 Rabbit consumer、改变 ACK 或写入 Redis/TD。

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

当前阶段：M0 的 current-release source/action authority 已形成审计证据；M1 已有 `SessionRuntimeCoordinator`、真实 Q2/Auction/Opening 只读 Shadow，但尚未形成替代验收；M2 的准备组合与 `HISTORICAL/REPLAY/LIVE` 时间模式已实现，真实 Q2 live admission 仍因上游 stale/存储滞后阻塞，昨日涨停/热板 producer metadata 也未在生产 release 闭环；M3 已有受控节点 Shadow，但跨重启持久身份和正式 source freeze ownership 仍未迁移；M4 已有窄范围 build-only Auction fact report projection，完整 legacy report/effect parity 仍未关闭；M5 的 Q2Frame/TD event-time replay 已有，但 Rabbit arrival/batch replay 继续延期。既有测试数量不代表上述迁移阶段通过。

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

正式验证使用服务器Python3.12固定提交副本。当前已有只读 Q2/Opening/Auction runner、M2 reference preparation/readiness runner、M1 morning shadow 和 M4 build-only report projection；这些命令仍是隔离验证入口，不是生产 owner。服务器没有redis-cli，使用既有venv的redis客户端，不为探查安装新服务。

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
