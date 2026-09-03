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

现有 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 生产链路保持在本项目之外。当前已实现 Q2 projection Adapter、fixture vertical slice 和事实切片；Q2Frame、TD event-time replay Adapter 按后续步骤接入，不把尚未验证的能力当成当前完成项。

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
