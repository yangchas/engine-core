# Auction Rule Candidate Audit

日期：2026-09-07（Asia/Shanghai）
分支：`codex/feature-auction-shadow`
范围：选择第一条业务 Shadow 规则；不迁移正式策略。

## 结论

本轮没有发现可以直接标记为 `VERIFIED` 的竞价强弱或转强/转弱策略规则。
`engine_core` 继续只输出事实级 `FACT_ONLY/OBSERVE`。

## 候选一：`classify_opening_entry_behavior`

当前代码位置：

```text
engine_next/strategy_skill_layer/stock_behavior.py
engine_next/strategy_skill_layer/local_strategy_framework.py
engine_next/strategy_skill_layer/local_decision_layer.py
```

当前行为包含：

```text
high_open_distribution
low_open_repair
volume_confirm
limit_attack
weak_follow
mixed
```

主要输入：

```text
open_pct
current_pct
amount_2m
auction_amount
speed_1m
is_locked
touched_limit_today
amount_2m_floor
```

当前证据：

| 项目 | 状态 | 说明 |
|---|---|---|
| 当前 `engine_next` consumer 引用 | OBSERVED | 被 stock/theme 本地策略层调用 |
| 单元测试 | OBSERVED | `engine_next/tests/test_stock_behavior.py` 使用合成 Snapshot |
| 真实输入 fixture | UNKNOWN | 尚未绑定真实 Q2/TD 行和 lineage |
| 字段单位与 source contract | UNKNOWN | `amount_2m`、`auction_amount` 尚未纳入 engine_core 当前契约 |
| 状态生命周期 | UNKNOWN | 创建、更新、锁定、失效和跨日行为未闭环 |
| 09:20→09:24 适配性 | NOT_APPLICABLE | 该规则主要消费开盘后字段，不是当前最小竞价段事实 |
| parity status | UNKNOWN | 暂不允许迁移为新 Strategy |

因此本候选只作为后续开盘审计入口，不进入当前 Auction Shadow 实现。

## 明确排除：旧 `C/analysis.cpp` 阈值

旧代码存在：

```text
bid_amount > ask_amount * 1.5
```

但当前没有证据证明：

```text
它仍属于 cobra-ion 当前生产 consumer；
金额单位与当前 t1-v2 字段完全一致；
该日志分支是正式策略决策；
它具备可追溯的状态生命周期和正式输出。
```

状态保持：

```text
parity_status = UNKNOWN
```

不得直接实现为 `AuctionShadowStrategy` 的强弱判断。

## 当前可迁移对象

当前唯一已闭环的对象仍是事实级相邻段比较：

```text
price_delta_milli
amount_delta_yuan
rest_bid_delta_yuan
rest_ask_delta_yuan
pressure_delta_yuan
coverage/status/provenance
```

对应实现为 `AuctionFactShadow`，固定输出：

```text
FACT_ONLY / OBSERVE
```

## 下一步证据要求

选择第一条真正的 Shadow 规则前，必须补齐：

```text
当前生产 consumer
真实字段与单位
触发时间
状态创建/更新/锁定/失效
真实正例
真实反例
PARTIAL/MISSING 案例
Legacy differential oracle
```

在这些证据闭环前，不新增策略结论、不影响正式 engine-next 输出。
