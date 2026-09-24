# TD/Rabbit Phase P — 片内业务屏障实验审计

审计时间：2026-09-25 01:00（Asia/Shanghai）<br>
状态：`BARRIER_AWARE_WHOLE_SLICE_REAL_PASS_WITH_LIMITS`<br>
主线状态：`PHASE_P_PARTIAL`

## 目的

Phase P 的第一次整片实验把每个 3 秒 SELECT 返回的全部 rows 一次提交给
t1-v2，最终 Q2 相同，但 09:20/09:24 快照包含了片末的后续 tick。此次实验
只在 validation 副本中验证修正假设：保持“一次 SELECT 一个 3 秒片”，不按
大小切 chunk；当片内跨越业务屏障时，只按事件时间划分处理先后，并在必要时
插入虚拟 Clock。生产源、consumer、ACK、TD writer 和服务均未改变。

## 实验输入和边界

- 真实 TD：`market_data1.stock_tick_v2`
- 交易日：`2026-09-24`
- 窗口：`[09:15:00,09:25:09)`，半开
- TD：每个 3 秒片一次 `SELECT`，只读
- validation binary：
  `/home/exedev/validation/td-rabbit-phase-p-barrier-build-20260925T001500+0800/t1_v2_barrier`
- binary SHA-256：
  `8224c2d2f150ce474bb32e37263d0bb73244f76523ab6f8b2c6449622ac341a2`
- Redis：DB13，前缀 `task009pbarrier:`；不是生产 DB0
- TD 写入：关闭；Rabbit consume/ACK：未发生

实验副本仅修改 `td_replay_tick_source.cpp` 的 pending batch 处理，屏障为：

```text
09:20:03
09:24:10
09:25:06
09:32:10
```

屏障只影响处理顺序，不重新查询、不丢 tick、不按固定行数拆分。生产源码和
当前服务没有被写入或重启。

## 真实运行结果

```text
batches                204
source_in/ticks        210730 / 210730
source_reject          0
clocks                 1
redis_cmds             423022
td_sql                 0
ack                    0
quote_state_committed  210692
max_batch_ticks        5169
exit                   0
```

隔离 Redis 结果：`5233` 个前缀 key，其中 `5222` 个 Q2 hash；生产 DB0 对该
前缀命中 `0`。完整摘要和运行证据见：

`/home/exedev/validation/td-rabbit-phase-p-barrier-run-20260925T010000+0800/`

## 与 exact t1-v2 基准的逐项比较

基准为 Phase M 的 Redis DB1/`task009m:`，同一真实日期和窗口。

- Q2：两边 `5222` symbols，规范化 Q2 SHA
  `c30129286aabada2a04ea5bbe2a2f03c57c0dbb44f0a2fe9630b5cafbc801cef`，相等。
- 0920 legacy auction：相等；`n=4869`，`ts=1790212803000`。
- 0924 legacy auction：相等；`n=5116`，`ts=1790213050000`。
- 0925 legacy auction：相等；`n=5208`，`ts=1790213106000`。
- latest projection 和 0925 anchor：相等。
- DB13 Core 只读回放：`5222/5222` observed、coverage `1.0`、missing `0`、
  549 stale；两次 projection hash 均为
  `fa978984f60ec345edcf8134c0a6a3834df2e9ce3cbf7ec9d7d07acf18f4ac5c`，抽样
  Engine 结果稳定，决策状态为 `FACT_ONLY`。

这验证了一个具体因果链：原始整片一次提交时，09:20 候选是 `n=4875,
ts=1790212805000`、09:24 候选是 `n=5117, ts=1790213051000`；加入片内
业务屏障后恢复为 exact 基准。修复的是 batch logical time 的 look-ahead，
不是 Redis 数据或 TD 行数。

## 结论

```text
TD_3S_SELECT_BOUNDARY                 PASS
ONE_SLICE_INPUT_WITHOUT_SIZE_CHUNK    REAL_VALIDATION_PASS
IN_SLICE_BARRIER_ORDER                REAL_VALIDATION_PASS
Q2_SEMANTIC_PARITY                    PASS (same date/window)
0920_0924_0925_AUCTION_PARITY         PASS (same date/window)
CORE_REDIS_READBACK                   PASS_WITH_PARTIAL_FRESHNESS
PRODUCTION_REDIS_SIDE_EFFECTS         NONE_OBSERVED
RABBIT_DELIVERY_EQUIVALENCE           UNKNOWN
HISTORICAL_ARRIVAL_ORDER              UNKNOWN
HISTORICAL_AVAILABLE_AT               UNKNOWN
NORMAL_OPENING                        UNPROVEN
M3_1_NORMAL                           BLOCKED
TD_WRITE_HEALTH                       UNPROVEN
```

这不是 TASK-009/Phase P 的最终生产等价通过。它证明了“一个 3 秒读片作为
Rabbit 同形输入”的实现假设在一个真实日期上可以通过业务屏障校正；尚未证明
真实 Rabbit 的 delivery membership、3–4 批到达顺序或历史 `available_at`。
整片结果仍需在第二个真实日期复测，并在主线审计通过后才可把 validation
逻辑移入受控 replay runner。不得部署、重启服务或修改 live consumer。

## 下一步门禁

1. 保留此实验二进制和 Redis DB13 证据，不删除或覆盖。
2. 在另一个真实交易日重复同样窗口，比较 09:20/09:24/09:25 和 Q2。
3. 增加一个只读源统计：每个 3 秒片的 row 数、distinct symbol 数、片内同股
   多时间 tick 数，验证“时间片输入”事实，不预先去重改变 Q2。
4. 第二日期通过后，才在 engine_core 的 replay contract 中固定“片内业务屏障”
   语义；Rabbit arrival 和 `available_at` 继续保持 `UNKNOWN`。

本审计使用 ECC `contract-first` 与 `production-audit` 约束，所有写入均为
用户批准的隔离 Redis DB，TD 仍为 SELECT-only。
