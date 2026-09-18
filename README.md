# engine_core

engine_core 是独立的行情驱动确定性计算内核。

首期最小闭环：

    Redis Q2
    -> Q2 Adapter
    -> CurrentMarketState
    -> 一个 Window
    -> EngineSnapshot
    -> Empty FrozenDataBundle
    -> ProbeStrategy
    -> stdout/trace

现有 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 生产链路保持在本项目之外。当前已实现 Q2 projection Adapter、fixture vertical slice、事实切片、Q2Frame replay 和 TD event-time replay；后者只保证 deterministic event-time，不恢复 Rabbit arrival/batch，不做 watermark 或 late correction。

## 当前实施状态

`codex/fix-foundation-wheels` 已完成第一批独立轮子；当前集成分支为
`codex/feature-session-engine-integration`，在不改生产链的前提下继续验证旧读取边界：

- Contract：`deep_freeze`、严格 canonical JSON、semantic/evidence hash、C 兼容 `trunc_div`。
- Q2：Field Contract、纯 `normalize_q2`/`validate_q2`、`classify_equity`、投影构造和 freshness 门禁。
- Window：半开区间、空窗口质量、单一 revision、`finality/origin`。
- Data：`PreviousDayStatsFunction`、薄 `ProviderResult` 包装、`TemporalDataGuard`。
- Facts：纯盘口压力、`SegmentFrame`、相邻段比较；不包含策略结论。
- Opening：从已部署 `engine_next` 提取的单股开盘涨幅、delta、符号状态和独立
  limit-state 事实，并提供 `build_opening_transition_fact` 组合竞价/开盘变化；仍不包含
  策略阈值或外部 I/O。

这些轮子均可不启动 Engine、不连接 Redis/TD 单独测试。当前版本新增版本化交易日快照：
运行时只读取离线生成的快照，`PreviousDayStatsFunction` 从请求交易日通过唯一日历
authority 派生上一交易日，不接受调用方注入的 expected date。Engine 集成目前提供
内存确定性队列、signal 幂等/冻结、evaluation ownership、frontier 防倒退、同刻因果
drain 和 Probe 调用；不包含持久化恢复或 Rabbit 接管。Real Data Probe 证据见
`docs/evidence/real_data_probe/`；当前仍不宣称 Rabbit arrival/batch replay 或完整策略迁移。

旧 `engine_next` 的窄竞价快照读取路径可用
`examples/run_engine_next_auction_loader_probe.py` 在 Redis 写保护下单独审计；
它只调用 `IntradayDataHub.load_auction_snapshots()`，不构造旧
`IntradayContextBuilder`。截至 2026-09-13，Cobra-ion 真实 Redis 验证无写入，
但历史 `0920/0924/0925` 结果为空（快照已过期或 `top_amount` 为空），因此这只是
读取边界证据，不是非空历史快照可用性证明。旧 context builder 仍有被阻止的写尝试，
不能直接作为 engine_core 的只读 Provider。

当前第一条 Auction Shadow 已接入 Core 的 Engine 组合边界：
`AuctionShadowStrategy` 只按 `PRE_AUCTION_0915`、`AUCTION_0920`、`AUCTION_0924`
三个明确锚点调用既有 `AuctionFactShadow`/`SegmentFrame`/`SegmentComparison` 轮子，
输出仍固定为 `FACT_ONLY/OBSERVE`，且不含阈值、候选、买卖或外部副作用。该类是
只读迁移适配，不替代 `engine-next` 的生产 owner；真实 0920/0924/0925 投影和
旧消费者的正式买盘阈值、转强/转弱规则仍需 parity 证据后才能迁移。

当前又增加了 `AnchorDeltaShadowStrategy` 这一条更窄的事实迁移切片：它只声明
`AUCTION_0920→AUCTION_0924` 与 `AUCTION_0924→AUCTION_0925` 两个相邻锚点，
委托已验证的 `build_anchor_delta_evidence` 计算价格/金额/买卖盘差值，输出
`FACT_ONLY/OBSERVE`、semantic/evidence/submission 身份，不产生任何策略结论。
默认相邻 pair 允许共享 `AUCTION_0924`，但重复 pair、跨 session 或冲突快照会拒绝。
该切片已在 Cobra-ion 的真实 TD `auction_snapshot_v2` 只读行上验证；这仍不代表
Rabbit batch、source-freeze ownership 或 engine-next 替代已经通过。

`examples/run_real_auction_engine_shadow.py` 提供一个有界验证入口：将真实
TD `auction_snapshot_v2` 投影适配为 canonical market projection，按每个业务锚点
提交 `MARKET_UPDATE` 与 `TIMER`，再由同一个 `DeterministicEngine` 调用该事实
Shadow。它只证明真实投影经过公开 Engine signal path 后与纯事实轮子保持 semantic
hash 一致；仍是单股票、TD SELECT-only、FACT_ONLY，不是全市场生产循环。

Opening 迁移从同一原则开始：`build_open_fact` 只计算可复核的单股事实；
`build_opening_transition_fact` 只组合已归一化的竞价/开盘变化；`change_pct`
使用百分数单位，`limit_state` 保持独立状态，不由涨幅推断。真实 Redis Q2 旁路验证脚本
为 `examples/run_real_opening_facts.py`，仅执行 `SMEMBERS/HGETALL`，不写生产数据。

`examples/run_live_morning_shadow.py` 在每个 Core-owned 节点（`AUCTION_0926`、
`OPENING_0932`）各做一次有界的 Q2 readiness 复检，并把结果写入节点证据；这不是
高频轮询或第二套调度器。Q2 复检失败时，竞价节点仍保留独立的 TD 事实路径并显式记录
错误；开盘节点依赖 Q2，因此继续 fail-closed。Q2 readiness 证据不改变 TD 事实的
semantic hash，也不把 Q2 偷换成竞价输入。

`examples/run_m3_auction_followup_shadow.py` 是 09:24/09:25 的窄节点验证入口：
它只接受调用方已经捕获的前置 projection，使用现有 session/timer/Engine 组合做一次
只读节点消费；正常运行窗口外不会读取 Redis，恢复运行不会用盘后数据回填旧锚点。它
只证明节点接入边界，不转移 t1-v2 的 09:20/09:24/09:25 source-freeze owner，
也不替代三锚点 `AuctionFactShadow` 的事实验收。

`SessionRuntimeCoordinator` 是 M1 的最小组合边界：它只锁定一个明确的交易日和
calendar/session identity，调用既有 readiness/timer 纯轮子，返回可提交或应延后的
`TimerFiring`，并在调用方确认 Engine 接收后记录一次性内存状态。它不读 Provider、
不持久化、不提交 Rabbit/Redis/TD、不发送 effect；因此可以先用于 Core Shadow，再
替换旧启动协调逻辑。`PARTIAL` readiness 不被自动升级为 `READY`，节点消费者仍需按
自身事实要求决定是否接受 stale/partial 输入。

该入口可显式加 `--prefetch-auction-references`，在启动前沿用既有 Redis/TD 只读访问路径
准备一次冻结的竞价参考数据；准备结果会进入启动 readiness 和后续节点的 Engine
`DATA_READY` 绑定。默认不启用，且不会写 Redis/TD、接管 Rabbit 或触发 effect。

`examples/run_real_opening_engine_shadow.py` 使用同一公开 Engine signal path
验证真实 Redis Q2 的单股开盘事实。它只读取 `SMEMBERS/HGETALL`，将 Q2 状态
提交为 `MARKET_UPDATE` 和开盘 `TIMER`，输出 `OpeningShadowStrategy` 的
`FACT_ONLY` 结果；速度字段不在单位未证明时强行映射，仍不产生策略结论。

## 开发

    python -m pytest -q
    python examples/run_vertical_slice.py

正式 Linux 运行使用 Python 3.12。Windows 仅用于开发、单元测试和回放。

默认 `pytest` 是离线合同测试：它不会连接 Redis、TD、Rabbit 或第三方网络源。
其中部分 Golden fixture 来自真实生产数据，但仍是冻结文件。服务器在线验证必须显式运行
`examples/run_live_q2_probe.py`（显式传入 freshness budget）和
`examples/run_real_reference_probe.py`，并把结果作为独立 evidence；真实 opening probe
`examples/run_real_opening_facts.py` 同样必须显式传入 `--stale-after-ms`，避免周末或停牌时
把旧 Q2 误报为 fresh。探针自己的 pytest 文件只使用 fake client 验证探针合同，不能冒充在线
连接测试。

Cobra-ion 在线探针必须使用生产共享 Python 3.12 venv（当前路径为
`/home/exedev/services/engine-next/shared/venv/bin/python`）。系统
`/usr/bin/python3.12` 不保证安装 `redis`/`taos`，它只能用于无外部依赖的
compile/test 检查；如果在线探针导入依赖失败，先修正运行时解释器，不要把依赖缺失误判为
Redis/TD 数据源故障。在线运行仍必须写入隔离 validation 目录，不得写生产服务目录。

如果需要在非生产隔离环境运行在线探针，可先在该隔离环境安装项目的 `live` extra：

```bash
python -m pip install -e '.[live]'
```

这只适用于临时验证副本；不要为了让生产旁路通过而修改或安装生产服务的环境。生产
Cobra-ion 优先复用已经验证的 engine-next 共享 venv，并在证据中记录 Python、依赖版本
和解释器路径。

当前真实验证范围和未迁移能力见：

- `docs/evidence/real_data_probe/20260909-real-integration-audit.md`
- `docs/evidence/real_data_probe/20260910-preopen-readonly.md`
- `docs/ENGINE_NEXT_REAL_DATA_HANDOFF.md`

## Git 约定

- main：可发布基线。
- codex/feature-<name>：新能力。
- codex/fix-<name>：缺陷修复。
- codex/test-<name>：测试和 fixture。
- codex/docs-<name>：文档和证据。
- 提交格式：type(scope): summary。
- 首期提交类型使用 chore、feat、test、fix、docs。
- 不在本仓库提交父仓库旧项目改动。
