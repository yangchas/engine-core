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

`examples/run_real_auction_engine_shadow.py` 提供一个有界验证入口：将真实
TD `auction_snapshot_v2` 投影适配为 canonical market projection，按每个业务锚点
提交 `MARKET_UPDATE` 与 `TIMER`，再由同一个 `DeterministicEngine` 调用该事实
Shadow。它只证明真实投影经过公开 Engine signal path 后与纯事实轮子保持 semantic
hash 一致；仍是单股票、TD SELECT-only、FACT_ONLY，不是全市场生产循环。

Opening 迁移从同一原则开始：`build_open_fact` 只计算可复核的单股事实；
`build_opening_transition_fact` 只组合已归一化的竞价/开盘变化；`change_pct`
使用百分数单位，`limit_state` 保持独立状态，不由涨幅推断。真实 Redis Q2 旁路验证脚本
为 `examples/run_real_opening_facts.py`，仅执行 `SMEMBERS/HGETALL`，不写生产数据。

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
