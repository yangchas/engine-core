# 测试真实性审计（2026-09-13）

## 结论

当前默认 pytest 套件是**离线、可重复的合同/fixture 测试**，不是实时数据连接测试；真实数据验证通过独立的 Cobra-ion 只读命令执行。两类测试的职责不同，不应合并为一个“全绿即生产可用”的结论。

## 本次证据

- 代码仓库：`engine_core`
- 当前提交：`2375e6be8ab10a2d380597d60a38b9e5272d66f5`
- 本地命令：`python -m pytest -q -p no:cacheprovider`
- 本地结果：`306 passed`
- 本地附加验证：`python -W error -m pytest -q -p no:cacheprovider`、`compileall`、`git diff --check` 均通过。
- Cobra-ion 同一代码归档、Python 3.12.3：`306 passed`、`compileall` 通过。

## 测试分层

### 1. 默认 pytest：离线合同测试

默认套件使用 `FakeRedis`、静态 provider、captured JSON/JSONL fixture 和纯函数输入。它覆盖：

- canonical/deep-freeze/hash/trunc-div；
- Q2 normalize/validate/classify/projection；
- Window、Clock、Calendar、Session、Timer；
- DataResult、TemporalDataGuard、FrozenDataBundle、DATA_READY ownership；
- Segment/Comparison/Auction fact shadow；
- Q2Frame 与 TD event-time 的确定性逻辑；
- 旧模块的受控 parity helper。

默认 pytest **不会**自动连接 Redis、TDengine、RabbitMQ、BaoStock、开盘啦、问财或 THS，也不会触发 SMTP、Redis/TD 写入或 Rabbit ACK。

### 2. Cobra 真实只读探针：独立于 pytest

已经执行过的真实验证包括：

- TD `auction_snapshot_v2`：读取 600519 的 0920/0924/0925 三锚点并重建事实；
- TD `daily_kline`：读取昨日数据，因没有历史 `available_at` 证据按合同返回 `UNAVAILABLE`；
- Redis Q2：周日无 `q2:active:*`，结果为 `MISSING/EMPTY_UNIVERSE`，没有宣称 live coverage；
- BaoStock、开盘啦、问财、THS、Kaipan：真实连接探针 6/6 成功，但只有 BaoStock 日线同时闭合请求/返回交易日；其余保持 `OBSERVED`，不能直接作为历史 runtime/replay 输入。

这些命令只读运行，未改 Redis/TD、未消费 Rabbit、未发送通知。具体 artifact 和 SHA 见 `docs/evidence/real_provider_cross_source_audit_20260913.md`。

## 准确性评价

当前测试边界没有伪造：fixture 测试确实验证了纯轮子；Cobra 命令确实验证了真实连接和真实返回。但以下结论仍不能从 306 passed 推出：

1. 生产 Rabbit 到 t1-v2 的 batch/decode/ACK 无丢失；现有服务日志没有提供足够 batch 证据。
2. Redis Q2 是不可变历史快照；它仍是运行时 projection。
3. TD `daily_kline` 有可证明的历史 `available_at`；当前为 UNKNOWN。
4. 旧正式竞价/开盘策略已迁移；阈值、状态生命周期和 consumer oracle 尚未闭环。
5. `engine_core` 已可替换 `engine_next`；当前没有生产 service、报告 owner 或 effect 接管。

## 下一步硬门槛

下一个交易日只做读路径和证据采集：

```text
Redis Q2
→ engine_core projection/Engine snapshot
TD auction snapshot/raw tick
→ fact shadow
旧 engine_next loader（GuardRedis）
→ bounded comparison
```

需要把未知收敛到明确边界：

- runtime batch membership：`PASS` 或 `UNKNOWN`；
- Redis/TD projection：字段 authority 可比时才比较；
- engine_next loader：读取事实与 core shadow 的 first divergence；
- 所有函数先完成静态副作用审计，再允许现场调用。

在上述链路和一个已验证旧策略规则完成 differential test 之前，不得宣布 core 可替代 next，也不得把离线测试数字当作生产验收。
