# TD/Rabbit Phase P — 同秒屏障纳入规则审计

审计时间：2026-09-25 04:49（Asia/Shanghai）  
交易日：`2026-09-23`  
阶段状态：`BARRIER_SECOND_INCLUSIVE=PASS_WITH_SOURCE_LIMITS`  
总状态：`PHASE_P=PARTIAL`

## 结论

已修复一个明确的竞价屏障顺序错误：时间秒截断后，业务屏障秒内的 tick 必须先被 t1-v2 处理，再由该 tick 的逻辑时间触发快照；不能在 3 秒切片右边界先发 Clock、把同秒 tick 留到快照之后。

同一修复在“发布计算源 + 当前 3 秒 TD 读取/屏障代码”的隔离验证副本上，通过真实 2026-09-23 数据与 DB5 基线的完整 Redis parity：Q2、0920/0924/0925 A2、legacy auction、latest、0925 anchor 均相等。当前开发分支的计算实现与发布源仍有已审计的语义差异；因此当前开发二进制的完整 Q2/A2 内容比较仍不通过，不能把本报告解释成整条回放已达到生产等价。

## 事实与根因

- 屏障候选时间按秒截断；TD 查询区间仍是 `[start_ms, end_ms)`。
- 原实现允许 `candidate_ms == slice_end_ms`，因此 09:20:03/09:24:10 位于前一片右边界时，先发 Clock，再查询含屏障秒 tick 的下一片。
- 原先切分条件使用 `event_second_ms >= barrier_timestamp_ms`，会把屏障秒内记录切到快照后。
- 这与系统既有秒截断规则和发布实现的 `<= barrier_hms` 行为不一致，会影响快照可见状态。

修复后行为：

1. 右边界屏障延至下一半开区间处理。
2. 下一片把 `event_second_ms <= barrier_timestamp_ms` 的全部真实 tick 放在屏障前。
3. 若屏障秒有真实 tick，则该 tick batch 携带 barrier 标记；`SnapshotTrigger` 在处理该 batch 时按截断秒触发，不额外插入 Clock。
4. 若屏障秒无真实 tick，则前序 tick 先完成，再产生独立 `Clock` 控制事件；Clock 不含行情 tick、不计入 market batch、不 ACK/拒绝消息。
5. Clock 时间与序号由 `control_ts_ms/control_seq_no` 承载，不借用空 `TickBatch` 伪装行情输入。

## 测试与提交

- 新旧边界断言先以测试失败暴露错误；第一次开发构建还暴露当前分支没有 `TickSourceStatus::Clock` 的端到端处理链。随后补齐 Engine、Pipeline、RuntimeLoop、TD writer 的 Clock 控制路径，并新增 Redis-only 时钟快照回归测试。
- `bash make.sh --dev-minimal --self-test`：PASS。
- `bash make.sh --full --self-test`：PASS；仅见已有 hiredis/TDengine 头文件地址检查 warning。
- `git diff --check`：PASS。
- 当前开发源码提交：`stock-situation-runtime` 分支 `codex/task-q2-pure-function`，commit `acf277bbba0d3bab90aa6550a23850e2c8aa7013`；尚未推送、合并或部署。

## 真实数据验证

### 发布计算源固定变量的验证副本

- 二进制：`/home/exedev/validation/t1v2-barrier-second-inclusive-20260925T041338+0800/t1_v2_inclusive_final`
- SHA-256：`0cf2c5f7f1cede0770d592d5e9c3b275ebc346385139f1cbc6cc26ff2fb83208`
- Redis 候选：DB15 / `task009pbarrierinc20260925T042334:`；基线 DB5 / `task009k:`。
- 输入：TD `market_data1.stock_tick_v2`，`2026-09-23 [09:15:00,09:25:09)`；读取按 3 秒半开片进行，TD 写入关闭。
- 只读复核结果：Q2 5222/5222 symbol/hash 完全相等；0920、0924、0925 的 A2 与 legacy auction 完全相等；latest 与 0925 anchor 完全相等；候选前缀 5233 keys，DB0 同前缀 0 keys。
- 验证副本的 legacy key 命名为 `legacy-auction:`、anchor 为 `anchor:`。首次只读核对用了错误 key 路径，得到空读回；按实际 key 名重查后确认完整相等。空读回已作废，不作为验收证据。

### 当前开发源码最终构建

- 二进制：`/home/exedev/validation/t1v2-barrier-clock-final-20260925T044741`
- SHA-256：`f85616d05ac23802ad2be1dbf56d8a75899d590b86199cf37fdbf8b50faf5e92`
- 运行证据：`/home/exedev/validation/t1v2-barrier-clock-final-real-20260925T044831+0800/`
- Redis 候选：DB15 / `task009clockfinal20260925T044831+0800:`；基线 DB5 / `task009k:`。
- 真实回放：`2026-09-23 [09:15:00,09:25:09)`；退出码 0，`source_in=ticks=212022`、`batches=204`、`clocks=1`、`clock_ts_ms=1790126706000`（09:25:06）、`source_reject=0`、`ack=0`、`td_sql=0`。Redis 写入仅在 DB15 唯一前缀；DB0 同前缀命中 0。
- 0920/0924/0925 的 `meta.n` 和逻辑触发时间与基线一致：`4873 @ 09:20:03`、`5100 @ 09:24:10`、`5208 @ 09:25:06`。这证明计数与触发时刻对齐，不单独证明完整 snapshot symbol set 相等。
- 当前开发源对照 DB5 的 5222 Q2 symbol 集合一致，但有 5206 个 Q2 hash 值不同；三组 A2 `top_amt/top_br/top_chg`、legacy `summary/top_amount` 和 anchor 内容亦不同。source-alignment 审计已确认开发分支与部署版的竞价计算、限价状态及 Engine clock/session 语义不完全相同；本次结果与该 source drift 一致，但不能把每个字段差异都归因于单一公式。

运行后只读检查：`t1-v2-live=active`、`engine-next=active`、两者 `NRestarts=0`；根分区约 22 GB 可用。未重启服务、未写 TD、未连接 Rabbit/消费/ACK。

## 验收状态

```text
HALF_OPEN_SLICE_RIGHT_EDGE_RULE           PASS
SAME_TRUNCATED_SECOND_TICK_BEFORE_TRIGGER PASS
EXPLICIT_CLOCK_CONTROL_PATH               PASS
SOURCE_ALIGNED_REAL_REPLAY_PARITY         PASS (validation copy)
CURRENT_DEV_BARRIER_COUNT_AND_TIME        PASS (count/time only)
CURRENT_DEV_FULL_Q2_AUCTION_PARITY        PARTIAL / NOT PASSED
CURRENT_DEV_SNAPSHOT_SYMBOL_SET_PARITY    UNPROVEN
RABBIT_DELIVERY_MEMBERSHIP_OR_ARRIVAL     UNKNOWN
HISTORICAL_AVAILABLE_AT                   UNKNOWN
M3_1_NORMAL                               BLOCKED
TD_WRITE_HEALTH                           UNPROVEN
PRODUCTION_SERVICE_SIDE_EFFECTS           NONE_OBSERVED
```

## 对齐与下一步边界

本轮只关闭“同秒屏障先后”这一条具体缺陷。TASK-009/Phase P 不完成，不能推进竞价迁移或策略重构，也不能把 replay 标成 NORMAL。

下一次如继续，应先针对当前开发分支与发布计算源的差异做只读字段级审计，确认目标语义和可追溯 golden source；然后单独提出范围，不要继续靠调整屏障来掩盖计算源差异。Rabbit arrival、delivery membership、completion watermark、historical `available_at` 仍须以真实采集证据解决。
