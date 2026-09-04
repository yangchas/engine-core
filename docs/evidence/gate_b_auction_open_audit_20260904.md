# Gate B：竞价 / 开盘行为审计（第一轮）

日期：2026-09-04（Asia/Shanghai）
分支：`codex/feature-engine-integration`
范围：09:20 → 09:24 → 09:25；只读考古，不迁移正式策略。

## 1. 审计边界与证据等级

本轮审计的目标是把旧系统的实际能力拆成：

```text
Input / Snapshot
    → Fact
    → Strategy / Decision
    → State lifecycle
```

旧源码是行为证据，不是新模块结构模板。`engine_core` 本轮没有导入、修改或运行旧项目业务代码；没有修改 RabbitMQ ACK、Redis/TD 写入或部署。

证据优先级：

1. cobra-ion 当前 `t1-v2` 发布包源代码（只读），作为当前生产链实现证据。
2. `engine_next` 运行时、C++ 旧实现和已有测试，用于发现历史行为与冲突。
3. 已捕获的真实 600519 竞价快照，用于字段和事实验收。

远端发布证据：

```text
host: cobra-ion
service: /home/exedev/services/t1-v2/current
release_commit: 6fb3164baab00d840886da5f056587ec32f3d86a
source_sha256:
  snapshot_trigger.cpp 078d8862fd55070a77c6d666a9056a4c8cb727b26e7ceb8d7ff52f00caee6753
  auction_calculator.cpp c3190f2b32c354b6cf67adc75381e21790b86c46e136e438e52854217ccbd932
  redis_v2_writer.cpp 9baa218adb6f00cf7ae85929659c74ff9d01ce4882bc9180cc1ef46d9e586940
  tdengine_v2_writer.cpp e6e64294bc64c0cdeccdc49341c3765442486331d8c92c33c0da05c7c94f3122
```

## 2. 时间节点与状态生命周期

| 能力 / 节点 | 旧系统行为证据 | 新内核可采用的语义 | 状态 |
|---|---|---|---|
| 09:15–09:20 试盘累计 | `C/t1.cpp`、`C/stock_analysis.h`；`MarketPhase::Auction` 持续更新 quote/auction state | 业务区间 `[09:15:00,09:20:00)`；只使用实际观测覆盖 | VERIFIED（行为） |
| 09:20 预览 | 当前 t1-v2 `SnapshotTrigger` 在 `hms >= 92003 && hms < 92400` 首次发出 A20；writer 写 `auction_snapshot_v2`、Redis `...:0920` 和 `latest` | `business_anchor=AUCTION_0920`、`business_anchor_time=09:20:00`、`source_record_time` 单独保留 | VERIFIED |
| 09:24 预览 | 当前 t1-v2 在 `hms >= 92410 && hms < 92500` 首次发出 A24；writer 写 `...:0924` 和 `latest` | `business_anchor=AUCTION_0924`、`business_anchor_time=09:24:00`、`source_record_time` 单独保留 | VERIFIED |
| 09:25 settling | 当前 t1-v2 在 `09:25:00` 到 `09:25:05` 不冻结 A25；`hms >= 92506` 才发出 A25 | 09:25 不是精确零秒快照；必须等待明确 settling barrier | VERIFIED |
| Python formal gate | `engine_next/app_main.py` 的 `AUCTION_FINALIZE_EARLIEST = 09:25:10`；controller 在此之前输出 waiting | 需区分“源快照可发出”和“Python 正式消费门禁” | OBSERVED |
| 旧实现时间边界 | 当前 t1-v2 在 `09:25:06` 通过 settling barrier 发出源 A25；同一发布包的 Python runtime 在 `09:25:10` 前保持 `auction_anchor_settling`，之后才发出正式消费事件 | 源快照形成时间与旧 Python 正式消费门禁是两层边界；新 Engine 尚不选择统一策略 | VERIFIED（边界已澄清，Engine policy DEFER） |
| 09:26 follow-up | Python `execute_auction_followup_0926` 重拉昨日涨停池和热点；仅当 anchor 缺失时恢复竞价 anchor | 作为恢复/补齐节点，不是新的正常 0920/0924 事实 | VERIFIED（旧运行时） |
| 开盘 cutoff | 当前 t1-v2 `SnapshotTrigger` 明确有 `09:32:10` opening cutoff；不属于本轮 09:20→09:25 最小案例 | 后续开盘审计单独处理 | VERIFIED（源代码） |

### 生命周期结论

```text
trade date 变化
    → t1-v2 清空 A20/A24/A25/opening cutoff 标记
Auction Tick
    → 更新可变 QuoteState.auction
节点触发
    → 写出带 tag 的历史快照
09:25
    → 形成正式 anchor；若缺失，09:26 允许恢复
```

当前 Redis `market:auction:{date}:latest` 会随 tag 更新，不能当不可变时间线；`market:auction:anchor:{date}` 只在 0925 路径形成。历史 `q2:active:{date}` 同样不是历史快照。

## 3. 当前使用的竞价字段契约

| 业务事实 | 旧生产定义 | 允许的解释 | 状态 |
|---|---|---|---|
| `P` 有效竞价价 | 当前 t1-v2：09:25 前要求一级 `bp1 == ap1 > 0`；09:25 后可用 `px_milli` | `price_milli`，不是自动回退的昨收价 | VERIFIED |
| `M` 匹配金额 | 09:25 前：`P × min(bv1,av1) × 100 / 1000`；09:25 后优先 `amt_yuan`，再用 `vol_units` 换算 | `auction_amount_yuan`；不能用盘中累计 `amount_yuan` 替代 | VERIFIED |
| `RB` 未匹配买方 | 当前 t1-v2：一级买价 × 二档买量 × 100 / 1000 | `rest_bid_amount_yuan`；二档数量按手 | VERIFIED |
| `RA` 未匹配卖方 | 当前 t1-v2：一级卖价 × 二档卖量 × 100 / 1000 | `rest_ask_amount_yuan`；缺字段不是零 | VERIFIED |
| Q2 `am/br/ar` | Redis writer 写入 `auction.match_amt_yuan/rest_bid_amt_yuan/rest_ask_amt_yuan` | canonical 名称使用 `auction_amount_yuan`、`auction_bid_amount_yuan`、`auction_ask_amount_yuan` | VERIFIED |
| 方向压力 | `RB - RA` | 盘口方向压力代理；不叫真实净流入、净买入或主力资金 | VERIFIED |
| `ts` | Q2 与 `stock_tick_v2.ts` 对齐 | `source_record_time_ms`；可用于 freshness/trade-date sanity | VERIFIED（边界） |
| `ts` 上游精确定义 | 是否交易所撮合时间、供应商采样时间或批次时间无证据 | 禁止解释为 exchange tick time 或 Rabbit arrival time | UNKNOWN |

当前真实 600519 fixture 的共同可观察字段只纳入：

```text
price_milli
auction_amount_yuan
auction_bid_amount_yuan
auction_ask_amount_yuan
```

`amount_yuan`、`volume_lots` 在 `auction_snapshot_v2` 中没有可靠值时保持 `None/UNAVAILABLE`，不因“真实数据”而补齐。

## 4. 09:20 → 09:24 真实事实样本

来源：`docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json`。

业务区间与锚点分离：

```text
Segment A business interval: [09:15:00, 09:20:00)
  observed_start = 09:15:08
  coverage = PARTIAL
  end anchor = AUCTION_0920, source_record = 09:20:03

Segment B business interval: [09:20:00, 09:24:00)
  observed_start = 09:20:03
  observed_end   = 09:24:10
  coverage = READY
  end anchor = AUCTION_0924, source_record = 09:24:10
```

600519 的源行：

| 锚点 | `P`（milli） | `M`（yuan） | `RB`（yuan） | `RA`（yuan） | source record |
|---|---:|---:|---:|---:|---|
| 09:15 observed | 1,297,540 | 389,262 | 0 | 0 | 09:15:08 `stock_tick_v2` |
| 09:20 | 1,299,600 | 2,599,200 | 0 | 129,960 | 09:20:03 `auction_snapshot_v2` |
| 09:24 | 1,297,540 | 7,006,716 | 648,770 | 0 | 09:24:10 `auction_snapshot_v2` |

可重复的段事实：

```text
Segment A:
  price return = +15 bp
  amount delta  = +2,209,938 yuan
  pressure      = -129,960 yuan
  coverage      = PARTIAL

Segment B:
  price return = -15 bp
  amount delta  = +4,407,516 yuan
  pressure      = +648,770 yuan
  coverage      = READY

A → B comparison:
  PRICE_WEAKER
  VOLUME_EXPANDING
  PRESSURE_IMPROVING
  breadth/theme = UNAVAILABLE
```

这些是事实比较，不是“竞价转强”“买入”“EV_HIGH”等策略结论。既有 `engine_core` 测试已验证该 fixture 在无 Engine、无 Redis、无在线 TD 的情况下重复生成相同结果。

## 5. 旧系统能力矩阵（第一轮）

| 能力 | 触发时间 | 输入 / 依赖 | 中间状态 | 输出 | 状态 |
|---|---|---|---|---|---|
| 竞价逐 Tick 累计 | 09:15–09:25 | RawTick 的价、一级/二级盘口、成交量/额 | `QuoteState.auction` 可变状态 | 当前 P/M/RB/RA | VERIFIED |
| 09:20 试盘快照 | 09:20:03 起 | A20 触发、股票过滤 | `emitted_a20` 一次性标记 | `auction_snapshot_v2` / Redis 0920 | VERIFIED |
| 09:24 接近结束快照 | 09:24:10 起 | A24 触发 | `emitted_a24` 一次性标记 | `auction_snapshot_v2` / Redis 0924 | VERIFIED |
| 09:25 正式快照 | 09:25:06（源）或 09:25:10（旧 Python/旧 C++ 门禁） | settling barrier、完整 auction state | `emitted_a25` / anchor | `auction_snapshot_v2` / Redis 0925/anchor | UNKNOWN（时间冲突） |
| 09:20 后累积形态分析 | 09:20 后 | 价、买卖金额、昨收 | `post_20_data_` | `analyzeAccumulationPattern` 结果 | OBSERVED，未迁移 |
| 撤单/订单流分析 | 每个 Auction Tick | 盘口、变化、金额 | `StockAuctionMetrics` 历史队列 | `analyzeOrderFlow` 结果 | OBSERVED，定义未闭环 |
| 竞价波动分析 | 约每秒 | `StockAuctionMetrics` | `last_analysis_time`、波动分数 | volatility/volatile pool | OBSERVED，未迁移 |
| 竞价板块聚合 | 09:25/09:26 | 热板、涨停池、竞价 rows | Python Context/Theme facts | 板块桶和开盘验证 | OBSERVED，超出本轮 |
| 开盘验证 | 09:30 后，当前 t1-v2 cutoff 09:32:10 | 开盘 2m、主题、前排/中位 | OpeningValidationBundle | confirmed/falsified/watch | OBSERVED，下一轮 |

## 6. Wheel-local Legacy Parity

| legacy capability | legacy location | new wheel / fact | behavior contract | parity_status | evidence |
|---|---|---|---|---|---|
| Q2 `amt` 累计元 | `C/t1_v2/redis_v2_writer.cpp` | `normalize_q2` | 保留 `amount_yuan`，不当作竞价 M | MATCH | 远端 release 6fb3164 |
| Q2 `vol` 累计手 | `C/t1_v2/redis_v2_writer.cpp` | `normalize_q2` | canonical `volume_lots`；不写 shares 别名 | MATCH | `docs/PROJECT_KNOWLEDGE.md`、Q2 fixture |
| Q2 `am/br/ar` | `C/t1_v2/redis_v2_writer.cpp` | Q2 projection / SegmentFrame | 分别表达 M/RB/RA | MATCH | 远端 writer、真实 auction fixture |
| 竞价方向压力 | `C/t1_v2/auction_calculator.cpp` | `compute_resting_order_pressure` | `RB - RA`，缺一侧为 UNAVAILABLE | MATCH | 远端 calculator、`tests/test_real_auction_fixture.py` |
| 半开业务窗口 | 旧源以 delayed anchor 写快照 | `WindowSpec` / `SegmentFrame` | `[start,end)` 与 `source_record_time` 分离 | INTENTIONAL_CHANGE | 真实 fixture；避免延迟改变业务区间 |
| `ts` 强语义 | 旧链只提供 source timestamp | `source_record_time_ms` | 只承诺可用于 freshness/sanity，不承诺 exchange/Rabbit order | INTENTIONAL_CHANGE | Q2/TD alignment evidence |
| 09:25 正式门禁 | t1-v2 09:25:06 vs Python 09:25:10 | Session/Engine integration | 未统一前禁止宣称单一正式时刻 | UNKNOWN | 远端 snapshot trigger、旧 app_main |
| 竞价“转强/转弱”阈值 | `C/t1.cpp`、旧 Python 多处分散 | 暂无新 Strategy | 必须先按能力/规则核对，不能由事实标签直接升级 | UNKNOWN | 未发现已闭环 differential oracle |

说明：`INTENTIONAL_CHANGE` 不是复刻旧 bug；它表示新内核更精确地隔离业务区间、源观测时间和时间能力边界。

## 7. 本轮不作为策略迁移的旧规则

旧 `C/analysis.cpp` 存在 `bid_amount > ask_amount * 1.5` 的“买盘强”日志分支，另有 `checkLimit`、撤单和波动阈值。但当前证据不足以证明：

```text
该实现仍是当前生产正式路径；
金额单位和盘口语义与当前 t1-v2 完全一致；
该日志标签就是竞价策略的正式决策规则；
```

因此本轮标记 `OBSERVED/UNKNOWN`，不迁移为 `AuctionShadowStrategy`，不把旧日志当 oracle。

## 8. 下一条最小 Shadow 规则候选

当前唯一适合立即进入 Shadow 的最小对象是**事实级锚点比较**，不是交易结论：

```text
输入：同一 symbol 的 AUCTION_0920 与 AUCTION_0924
输出：
  price_delta_milli
  amount_delta_yuan
  rest_bid_delta_yuan
  rest_ask_delta_yuan
  pressure_delta_yuan
  coverage/status/provenance
```

它可以由已冻结的 `SegmentFrame` + `compare_adjacent_segments` 直接产生，并用 600519 真实 fixture 做 differential。第一条真正带“转强/转弱”结论的 `AuctionShadowStrategy` 暂不宣称 VERIFIED，必须等 Gate B 的规则行具备明确 legacy consumer、阈值、状态生命周期和 fixture 后再实现。

## 9. 09:25 双门禁语义

当前发布包的两层时间语义已经核对清楚：

```text
SourceSnapshotGate:
  first accepted auction source tick at/after 09:25:06
  → t1-v2 emits A25 and writes the source snapshot path

LegacyConsumerGate:
  Python runtime before 09:25:10
  → waiting / no formal auction finalize event
  at/after 09:25:10
  → emits auction_anchor_finalize
```

这不是同一个时钟上的冲突，而是“生产源快照已形成”与“旧运行时允许正式消费”两个边界。`engine_core` 本轮不把任一边界偷偷改成统一新策略；未来 Engine Integration 需要显式选择或同时保留两者。

本轮事实级 Rule Matrix 与独立 oracle 差异测试见：
`docs/evidence/gate_b_rule_matrix_600519_20260904.md`、
`tests/test_gate_b_fact_differential.py`。

```text
Gate B 业务考古（09:20/09:24/09:25）  PASS（第一轮）
Q2 / M / RB / RA / pressure 事实边界     PASS
真实 600519 A/B/Compare                  PASS
09:25 双层时间边界                        VERIFIED（Engine policy DEFER）
竞价转强/转弱正式策略 parity               NOT YET VERIFIED
开盘 09:30/09:32 行为审计                 DEFER
```

下一步只做：

1. 选择一条有明确旧 consumer 的最小规则，补齐 Rule Matrix 和 fixture。
2. 用现有 `SegmentFrame` / `SegmentComparison` 先做事实级 Shadow，不修改 Foundation。
3. 现场捕获一次节点前参考数据 prefetch 的 `observed_at` 证据；不放宽 TemporalDataGuard。
4. 后续 Engine Integration 显式保留 source snapshot gate 与 legacy consumer gate；不在事实层合并二者。

仍然延期：Checkpoint、REPLAY_RECORDED、Rabbit direct ingestion、watermark、late correction、通用 fallback/workflow、正式 effect。
