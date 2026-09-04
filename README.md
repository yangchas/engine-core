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

现有 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 生产链路保持在本项目之外。当前已实现 Q2 projection Adapter、fixture vertical slice、事实切片和最小 Q2Frame + VirtualClock replay；TD event-time replay 仍延期，不把尚未验证的能力当成当前完成项。

## 当前实施状态

`codex/fix-foundation-wheels` 已完成第一批独立轮子：

- Contract：`deep_freeze`、严格 canonical JSON、semantic/evidence hash、C 兼容 `trunc_div`。
- Q2：Field Contract、纯 `normalize_q2`/`validate_q2`、`classify_equity`、投影构造和 freshness 门禁。
- Window：半开区间、空窗口质量、单一 revision、`finality/origin`。
- Data：`PreviousDayStatsFunction`、薄 `ProviderResult` 包装、`TemporalDataGuard`。
- Facts：纯盘口压力、`SegmentFrame`、相邻段比较；不包含策略结论。

这些轮子均可不启动 Engine、不连接 Redis/TD 单独测试。Engine 集成目前只提供
内存确定性队列、signal 幂等/冻结、frontier 防倒退和 Probe 调用；不包含持久化
恢复或 Rabbit 接管。Real Data Probe 证据见
`docs/evidence/real_data_probe/`；当前仍不宣称 Rabbit arrival/batch replay 或完整策略迁移。

## 开发

    python -m pytest -q
    python examples/run_vertical_slice.py

正式 Linux 运行使用 Python 3.12。Windows 仅用于开发、单元测试和回放。

## Git 约定

- main：可发布基线。
- codex/feature-<name>：新能力。
- codex/fix-<name>：缺陷修复。
- codex/test-<name>：测试和 fixture。
- codex/docs-<name>：文档和证据。
- 提交格式：type(scope): summary。
- 首期提交类型使用 chore、feat、test、fix、docs。
- 不在本仓库提交父仓库旧项目改动。
