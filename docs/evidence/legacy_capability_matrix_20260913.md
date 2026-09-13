# engine_next 旧能力对照矩阵（定向审计）

日期：2026-09-13  
主机：`cobra-ion`  
审计发布：`/home/exedev/services/engine-next/releases/20260903_e272842`  
审计目标：为第一条 Auction Shadow 迁移划定事实、Provider、策略和副作用边界。

## 结论

旧系统并不是单一的“竞价函数”。它至少包含四类能力：

1. Redis Q2/竞价投影读取；
2. 竞价快照事实与相邻锚点差分；
3. 通过 TD/网络 fallback 恢复并回写竞价 Anchor；
4. 主题/开盘验证和报告策略判断。

当前 `engine_core` 已闭环第 2 类的最小事实子集（P/M/RB/RA、pressure、相邻段比较），其余能力不能因为函数名相似而直接迁移。

## 生产入口与调用边界

| 能力 | 旧入口/函数 | 真实输入 | 输出/副作用 | 新 core 状态 |
| --- | --- | --- | --- | --- |
| 在线 Q2 | `IntradayDataHub.fetch_online_q2_rows` | Redis `q2:active:{date}` / `q2:{symbol}` | 当前行情投影；读取 | 已有 Q2 Adapter；实时状态，不是历史快照 |
| 竞价投影读取 | `IntradayDataHub.load_auction_snapshots` | Redis `market:auction:{date}:0920/0924/0925` | TopN/summary 行及相邻 delta；本次路径只读 | 可作 source-specific read adapter；不能当全市场 raw authority |
| 竞价 Anchor 恢复 | `IntradayDataHub.recover_auction_anchor` | Redis anchor/0925、TD fallback、问财 fallback | 可能 `SET market:auction:anchor:{date}`；存在外部 I/O | **禁止**作为 core 只读 Provider；副作用审计未通过 |
| 09:25 finalize | `AuctionRuntimeController.execute_auction_finalize_0925` | 昨日涨停、当日热板、Anchor、runtime summary | 刷新多个缓存并恢复 Anchor | 不迁移；保留为 legacy orchestration evidence |
| 09:26 follow-up | `execute_auction_followup_0926` | 同上，缺 Anchor 时继续恢复 | 可能回写 Anchor；刷新缓存 | 不迁移；需后续策略/副作用拆分 |
| 板块竞价 bucket | `build_auction_plate_bucket_stats` | `StockStateSnapshot`、hot plate、昨日涨停等 | 板块聚合、`expectation` 结论 | 事实与策略混合；当前无 consumer oracle，UNKNOWN |
| 竞价锚点 delta | `build_auction_snapshot_delta_stats` / `build_anchor_shadow_evidence` | 0924/0925 归一化行 | 金额/价格/买方 delta、展示信号 | source-formula 已对照；方向标签仍不等于策略 |
| 开盘验证 | `build_opening_validation_bundle` | 主题选择、前排/中位、2m 金额、开盘涨跌 | `confirmed/watch/falsified`、`attack/watch/avoid` | 含策略阈值；无真实 legacy oracle，UNKNOWN |
| 报告生命周期 | `ReportingLifecycle`、`ProductionReportingCoordinator` | `auction_facts_0926`、`opening_facts_0932` | Redis claim、邮件 eligibility、报告渲染 | 不属于第一条 core Shadow；必须保持 effect/read path 分离 |

## 旧时间节点的真实语义

| 节点 | 旧代码行为 | 可迁移事实 |
| --- | --- | --- |
| 09:20 | `t1_v2`/snapshot writer 形成 0920 投影；Redis 读取路径按 tag 加载 | 业务锚点 0920；source timestamp 需保留 |
| 09:24 | 形成 0924 投影；`load_auction_snapshots` 可计算 0920→0924 delta | 相邻 Segment 的候选端点 |
| 09:25:06 | t1-v2 settling barrier 形成 A25 源快照（来自既有生产审计） | source gate，不等同于 legacy consumer 已消费 |
| 09:25:10 | 旧 Python runtime 的正式 Anchor/分析路径最早允许执行 | consumer gate；不可反推同 batch |
| 09:26 | 旧 runtime 刷新昨日涨停、热板、Anchor、市场摘要并生成竞价报告输入 | 仍含可写 recovery，不可直接作为只读 core 节点 |
| 09:32:10 | `opening_facts_0932` 的正常报告窗口 | 开盘事实报告时点；策略结论需另有 oracle |

## 事实、策略和副作用拆分

### 可以作为当前事实层的内容

```text
P / M / RB / RA
pressure = RB - RA
amount_delta
price_delta
rest_bid_delta
rest_ask_delta
source_record_time
coverage / missing / unavailable
```

这些字段只有在同一 source formula、单位和缺失语义已确认时才可比较。`pressure` 是盘口代理，不是净资金流入。

### 目前不能直接迁移为策略的内容

```text
_is_turn_strong
_infer_expectation
bid_amount > ask_amount * 1.5
前排承接/中位扩散阈值
confirmed / falsified / attack / avoid
主题迁移、轮动和资金抽离结论
```

原因：当前缺少活动生产 consumer 的同输入 oracle、单位/phase 闭环或状态生命周期 fixture。旧源码中的阈值只能标记为 `OBSERVED`，不能宣称 `VERIFIED`。

### 明确禁止抽成 core Provider 的路径

`recover_auction_anchor()` 不是读取函数。它会按 Redis → 0925 → 预览 → TD → 问财的顺序寻找数据，并在部分成功路径执行 `SET` 归档。即使调用方只想“拿到一行数据”，也可能改变生产 Redis 状态；core 只能使用已冻结的只读投影或显式薄 Provider。

## Wheel-local parity 状态

| 旧能力 | 新对象 | 状态 | 证据/缺口 |
| --- | --- | --- | --- |
| 0920/0924/0925 端点差分 | `build_segment_frame` + `compare_adjacent_segments` | MATCH（事实子集） | 600519 真实 TD fixture、独立公式差异测试 |
| RB/RA pressure | `compute_resting_order_pressure` / OrderBookFacts | MATCH（代理语义） | C++ writer 与 TD snapshot 交叉证据 |
| Redis Q2 当前投影 | Q2 Adapter + `CurrentMarketState` | MATCH（实时投影边界） | Cobra 只读 Q2 probe；空 cohort 正确 MISSING |
| 竞价板块 bucket | 暂无 core 对象 | UNKNOWN | 旧实现含热板、昨日涨停、leader 和阈值混合；没有真实 consumer oracle |
| `turn_strong/turn_weak` | 暂无 core Strategy | UNKNOWN | 旧文件有规则，但不在活动 t1-v2 入口中闭环 |
| 开盘主题验证 | 暂无 core Strategy | UNKNOWN | 需要真实 09:25/09:32 配对、状态生命周期和 legacy differential |
| Anchor recovery/fallback | 暂不抽取 | NOT_APPLICABLE（core 首期） | 有 Redis/TD/网络写入风险，属于旧编排能力 |

## 下一条迁移门槛

第一条 Auction Shadow 仍只能从事实层开始。要把任意 `turn_strong`、主题验证或 `attack/avoid` 规则迁入，必须先取得：

```text
活动生产入口中的实际 consumer 调用位置
真实字段/单位/phase 语义
状态创建、更新、锁定、失效条件
至少一组真实正例和反例
PARTIAL/MISSING 行为
legacy differential oracle
```

在这些证据出现前，继续扩策略框架属于越界。当前正确动作是：

1. 保持 `engine_core` 的 P/M/RB/RA fact-only shadow；
2. 下一个交易日优先捕获 0920/0924/0925 的真实 Redis/TD/运行日志链路证据；
3. 仅对能闭环的第一个旧能力补最小 Fact/Strategy，不创建通用规则框架。

## 审计限制

- 本文读取的是 Cobra 上现有 release 源码和只读证据，不修改生产服务。
- TD 行顺序不代表 Rabbit arrival/batch 顺序；同毫秒同标的缺少 source sequence 时不推断因果。
- Redis 0920/0924/0925 当前只证明 TopN/summary 投影能力，不证明完整全市场历史快照。
- 本文不把旧实现的阈值、报告文案或历史 bug 复制成新合同。

## 审计源码身份（Cobra SHA-256）

```text
runtime/intraday_data_hub.py                         645ab676d921c6437b28efce3535b4ee32cac8483334610c5dc30e532f634c5b
runtime/controllers/auction_runtime_controller.py   c7793bbc1b2f5dafc5c7303de72aa8411e9122d60468dbfa9912edaad23802a5
runtime/production_fact_assembly.py                  3b145d6225031aacedf3a83f79bff139f51854b45809e469600cf6581180a2c8
strategy_skill_layer/auction_plate_buckets.py        c530b41b10643204eb89ce3b73b720e01caade4936643aa215948f686ab90af0
strategy_skill_layer/opening_validation_hub.py       1d6cc9c7fdb6fa4b554be24aeb80e89782a209b909cf7212344e334355812be8
runtime/reporting_lifecycle.py                       73b41002333a7563c156e10c620dbd80fb0a1d873f403f2d1cd5f6ff301c011a
```

上述文件均来自同一 release 目录；源码 SHA 仅用于绑定审计对象，不代表其中所有规则已经通过业务 oracle 验证。
