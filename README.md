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

`codex/fix-foundation-wheels` 已完成第一批独立轮子：

- Contract：`deep_freeze`、严格 canonical JSON、semantic/evidence hash、C 兼容 `trunc_div`。
- Q2：Field Contract、纯 `normalize_q2`/`validate_q2`、`classify_equity`、投影构造和 freshness 门禁。
- Window：半开区间、空窗口质量、单一 revision、`finality/origin`。
- Data：`PreviousDayStatsFunction`、薄 `ProviderResult` 包装、`TemporalDataGuard`。
- Facts：纯盘口压力、`SegmentFrame`、相邻段比较；不包含策略结论。

这些轮子均可不启动 Engine、不连接 Redis/TD 单独测试。当前版本新增版本化交易日快照：
运行时只读取离线生成的快照，`PreviousDayStatsFunction` 从请求交易日通过唯一日历
authority 派生上一交易日，不接受调用方注入的 expected date。Engine 集成目前提供
内存确定性队列、signal 幂等/冻结、evaluation ownership、frontier 防倒退、同刻因果
drain 和 Probe 调用；不包含持久化恢复或 Rabbit 接管。Real Data Probe 证据见
`docs/evidence/real_data_probe/`；当前仍不宣称 Rabbit arrival/batch replay 或完整策略迁移。

当前第一条 Auction Shadow 仍停在事实层：`AuctionFactShadow` 复用相邻
`SegmentFrame`/`SegmentComparison` 输出可追溯的 P/M/RB/RA/pressure 变化，固定为
`FACT_ONLY/OBSERVE`。旧系统的正式买盘阈值和转强/转弱规则仍待 legacy consumer
parity 闭环，不能从事实标签直接升级为交易策略。

## 开发

    python -m pytest -q
    python examples/run_vertical_slice.py

正式 Linux 运行使用 Python 3.12。Windows 仅用于开发、单元测试和回放。

默认 `pytest` 是离线合同测试：它不会连接 Redis、TD、Rabbit 或第三方网络源。
其中部分 Golden fixture 来自真实生产数据，但仍是冻结文件。服务器在线验证必须显式运行
`examples/run_live_q2_probe.py` 和 `examples/run_real_reference_probe.py`，并把结果作为独立
evidence；探针自己的 pytest 文件只使用 fake client 验证探针合同，不能冒充在线连接测试。

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
