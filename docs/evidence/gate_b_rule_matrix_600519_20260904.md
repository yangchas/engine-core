# Gate B：600519 最小竞价事实 Rule Matrix

日期：2026-09-04（Asia/Shanghai）
分支：`codex/feature-engine-integration`
范围：只核对 09:20 → 09:24 的已验证事实，不迁移“转强/转弱”策略结论。

## 证据边界

旧系统行为只按能力对照，不按旧函数一比一搬运。当前差异测试的 oracle 来自 cobra-ion 当前 `t1-v2` 发布源和同次只读捕获的真实 600519 行；它不导入或运行旧项目代码。

远端发布：`/home/exedev/services/t1-v2/current`，release commit `6fb3164baab00d840886da5f056587ec32f3d86a`。

Fixture：`tests/fixtures/facts/auction_600519_20260903.json`
原始证据：`docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json`

## 局部 Rule Matrix

| legacy_capability | legacy_location | new_wheel / fact | behavior_contract | parity_status | evidence | fixture |
|---|---|---|---|---|---|---|
| 竞价匹配金额在锚点间的观察值变化 | `auction_calculator.cpp` / `auction_snapshot_v2.match_amt_yuan` | `SegmentFrame.volume.amount_delta_yuan` | `M_current - M_previous`；字段缺失保持缺失；不回退到盘中累计 `amount_yuan` | MATCH | 当前发布源 + 600519 源行 | 600519 0920/0924 |
| 一级剩余买卖盘金额的锚点变化 | `auction_snapshot_v2.rest_bid_amt_yuan/rest_ask_amt_yuan` | `SegmentFrame.order_book` + comparison | 分别比较 `RB_current - RB_previous`、`RA_current - RA_previous`；不把缺失当零 | MATCH | 当前发布 writer + 600519 源行 | 600519 0920/0924 |
| 方向压力代理 | `auction_calculator.cpp` 的 `RB - RA` 语义 | `compute_resting_order_pressure` | `pressure = RB - RA`；只称盘口方向压力代理，不称净流入 | MATCH | 当前发布 calculator | 600519 0920/0924 |
| 相邻竞价段比较 | 旧系统分段输出与当前事实契约 | `compare_adjacent_segments` | 同 scope 且 `previous.end == current.start`；输出价格、成交、压力变化标签，不输出交易结论 | MATCH | Segment contract + local tests | 600519 A/B |
| 竞价“买盘强” | `C/analysis.cpp` `bid_amount > ask_amount * 1.5` | 暂无 Strategy | 需要确认当前生产 consumer、单位、状态生命周期和正式输出含义后才能迁移 | UNKNOWN | 旧日志分支，未形成闭环 oracle | 不适用 |

## 600519 独立 oracle

只对已验证字段执行最小差异比较。oracle 使用真实源行中的 canonical 数值，不使用新事实函数计算期望值：

```text
M(0920) = 2,599,200
M(0924) = 7,006,716
M_delta  = 4,407,516

RB(0920) = 0
RB(0924) = 648,770
RB_delta  = 648,770

RA(0920) = 129,960
RA(0924) = 0
RA_delta  = -129,960

pressure(0920) = 0 - 129,960 = -129,960
pressure(0924) = 648,770 - 0 = 648,770
pressure_delta = 778,730

P_delta = 1,297,540 - 1,299,600 = -2,060 milli
```

`pressure_delta` 是两个已观察锚点压力值的数值差；`PRESSURE_IMPROVING` 是其事实比较标签。两者均不等于资金净流入。

## 生命周期与状态

```text
AUCTION_0920 / AUCTION_0924 是源快照的业务锚点；
source_record_time 只描述实际源行时间；
相邻 Segment 使用业务区间 [start, end)；
Segment A 当前为 PARTIAL，Segment B 当前为 READY；
主题、市场宽度、龙头/跟随不进入本条最小 oracle。
```

本矩阵只证明事实轮子对已验证输入的解释一致。没有把旧日志阈值、撤单、波动或 09:25 正式决策门禁宣称为已迁移。
