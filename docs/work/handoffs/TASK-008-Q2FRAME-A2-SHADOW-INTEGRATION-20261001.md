# TASK-008 Q2Frame → A2 summary shadow integration

审计日期：2026-10-01（Asia/Shanghai）
状态：`STAGE_COMPLETE; TASK-008 remains PARTIAL`

## 本阶段范围

在既有 Q2Frame → 单 Engine auction shadow runner 的 09:25 报告中，增加一份由已消费 Q2 更新纯函数派生的 `Q2AuctionSummaryProjection`。它是附加的 `FACT_ONLY` 证据，不进入 Engine signal、Engine state、strategy input、Redis 或 TD。

本阶段没有访问 Redis、TDengine、RabbitMQ 或生产服务，没有修改 TASK-008 任务状态，也没有自动启动后续阶段。

## 实现

- Runner 在顺序消费 Q2Frame 时只保留每个 symbol 最新的原始 Q2 update；09:25 barrier 捕获时用现有 `normalize_q2` 与 `derive_q2_auction_summary` 构造附加汇总。
- 不使用 Q2Frame 全文件 symbol 清单作为 09:25 的 expected universe；`market_universe_coverage_status=UNKNOWN`、`q2_input_coverage=null`。
- 汇总输入内容引用冻结 Q2Frame SHA-256；来源时间、历史 available-at 和 Rabbit arrival 语义不被推断。
- 输出结构新增字段，runner contract 升版：auction V5、session/opening V4。

## 真实冻结输入验证

输入 Q2Frame：

```text
/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2frame.jsonl
SHA-256: 10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0
```

运行输出：

```text
/home/exedev/validation/task008-q2frame-engine-summary-20261001T021513+0800/auction_shadow.json
SHA-256: a655ea9cd9296b51d60f5b641f5437301587ade7b7cdc1620562d25efa2a458d
```

实际结果：603 frames、199,621 Q2 updates、5,220 observed symbols；同一冻结输入完成 ordered/repeat 两次运行，`deterministic=true`。09:25 barrier 包含全部 603 帧（数据最后时间为 09:25:02.000），Engine `engine_revision=reducer_revision=603`。

从 Q2 `am/br/ar` 推导的 5,210 候选 summary 与已冻结 2026-09-30 Redis A2 capture 中的十项聚合数值一致：

| 指标 | Q2 projection | Redis A2 frozen summary |
|---|---:|---:|
| stock_count | 5,210 | 5,210 |
| valid_stock_count | 5,030 | 5,030 |
| unavailable_stock_count | 180 | 180 |
| positive / high-open | 3,534 | 3,534 |
| negative / low-open | 785 | 785 |
| flat | 711 | 711 |
| limit up | 10 | 10 |
| limit down | 4 | 4 |
| auction amount (yuan) | 11,881,094,371 | 11,881,094,371 |
| limit-up seal amount (yuan) | 15,056,893 | 15,056,893 |

冻结 A2 summary anchor 为 `2026-09-30 09:25:06.026 +08:00`；Q2 最后逐股 source time 为 `09:25:02.000`，相差 `4,026 ms`。这是跨 capture 的值对齐，不是同一时刻或逐 tick parity。A2 capture 文件 SHA-256：`90a0f5b83aaabc69c762936d4e0454b84b89192e9b72549c5deb0be41d1fe2dc`；其 summary fixture 位于 `tests/fixtures/facts/auction_market_summary_20260930.json`。

## 解释边界 / UNKNOWN

- 这是一个交易日、一份冻结的 t1-v2 exact-release Q2Frame artifact 与一份独立冻结的 Redis A2 summary capture；不能据此证明跨日普遍等价。
- 本结果不证明 Rabbit arrival order、Rabbit delivery membership、历史 `available_at`、真实队列批次边界或同一时刻快照。
- 5,220 是该 Q2Frame 中观察到的 symbol 集合，不等同已证明的全市场 universe；投影仍明确输出 coverage `UNKNOWN`。
- 当前 Q2 候选规则根据可见的 `am/br/ar` 正值推断；t1-v2 A2 writer 另检查 Q2 不暴露的内部 `auction.ts_ms > 0`。本日数值一致不消除该规则差异。
- Q2Frame 完整双跑约耗时 24 分钟。功能双跑成功；耗时属于现有完整 Engine shadow 路径的成本观察，不作为本阶段正确性失败门禁，也未在本阶段进行性能优化。
- A2 capture 的 observation time 是 `09:25:20.028`，summary anchor time 是 `09:25:06.026`；不要混用两者。

## 验证

```text
Focused runner tests: 7 passed
Full suite: 754 passed, 3 protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
Production I/O imports in changed runner/projection path: none found
Production side effects: NONE_OBSERVED
```

## 阶段对齐

本阶段只补上 Q2Frame shadow 报告里的 Q2→A2 辅助事实汇总，并通过单日冻结数据对照；它没有完成 TASK-008 的整体目标，也不升级为 `READY` / `PASS`。下一步必须先复核本阶段 diff 与该 handoff 的事实边界，再由用户明确选择是否继续扩大到其他日期或其他开盘证据；不得自动进入策略迁移或生产接入。
