# engine_core Agent Rules

## 工作入口

- 开始任务前先读 docs/PROJECT_KNOWLEDGE.md。
- 已标记 VERIFIED 的事实原则上不重复探查。
- UNKNOWN 可以探查；OBSERVED、INFERRED 不得当成 VERIFIED。
- 新发现的长期知识必须沉淀；大体积原始证据放 docs/evidence/。
- 任务结束前检查是否需要更新长期知识。

## 固定红线

    Missing != Zero
    historical oracle != runtime input
    deterministic replay != production batch equivalence
    observed behavior != universal market rule
    current market state != query data
    DataFunction != DataProvider
    Fact != Strategy conclusion
    fallback source != fallback semantic
    same engine != same input fidelity
    DataFunction may block its worker
    DataFunction must never block the reducer

## 独立性

- engine_core 不导入、不复制、不修改旧项目源码。
- 旧 RabbitMQ/t1_v2/TD/Redis 链路只通过协议边界接入。
- 任何旧行为迁移先形成 fixture 和 evidence，再实现新代码。
- 本项目首期不接管 Rabbit ACK，不实现正式 effect。
