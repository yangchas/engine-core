# TD/Rabbit Phase P — 第二真实日期片内业务屏障实验审计

审计时间：2026-09-25 01:03–01:16（Asia/Shanghai）<br>
交易日：`2026-09-23`<br>
状态：`BARRIER_AWARE_WHOLE_SLICE_REAL_PASS_WITH_LIMITS`<br>
主线状态：`PHASE_P_PARTIAL`

## 目的与边界

这是第一次 barrier-aware whole-slice 实验后的第二个真实交易日复测。输入为
TD `market_data1.stock_tick_v2` 的 `[09:15:00,09:25:09)`，每个 3 秒半开
区间只执行一次 `SELECT`；片内不按行数切 chunk，只有 09:20:03、09:24:10、
09:25:06、09:32:10 等业务屏障影响处理先后。使用 validation 副本的 t1-v2
二进制，不修改生产源码、consumer、ACK、TD writer 或 systemd。

- binary：`/home/exedev/validation/td-rabbit-phase-p-barrier-build-20260925T001500+0800/t1_v2_barrier`
- binary SHA-256：`8224c2d2f150ce474bb32e37263d0bb73244f76523ab6f8b2c6449622ac341a2`
- Redis：DB14，唯一前缀 `task009pbarrier23:`；精确基准为 DB5/`task009k:`
- TD：SELECT-only；`REPLAY_WRITE_TDENGINE=false`
- Rabbit/ACK：未消费、未 ACK

## 真实运行结果

```text
batches                204
source_in/ticks        212022 / 212022
source_reject          0
clocks                 1
redis_cmds             425502
td_sql                 0
ack                    0
quote_state_committed  211932
max_batch_ticks        5110
exit                   0
```

逐片只读源统计另存于：
`/home/exedev/validation/td-rabbit-phase-p-barrier23-run-20260925T013000+0800/per_slice_stats_v1.json`。
203 个 3 秒片合计 212022 行，1 个空片（`09:25:06–09:25:09`），最大单片
5110 行/5106 symbols，24 个片含同一 symbol 的多个事件时间。屏障附近真实
统计为：`09:24:57–09:25:00` 4898 行、`09:25:00–09:25:03` 5071 行/5068
symbols（3 个 symbol 有多个事件时间）、`09:25:03–09:25:06` 1 行、
`09:25:06–09:25:09` 0 行。统计查询每次读取后即丢弃行，只保留该片摘要。

## 隔离 Redis 与 Core 读回

DB14 前缀共 5233 个 key，其中 5222 个逐股 Q2 hash。与同一真实日期、同一
窗口的 exact t1-v2 基准 DB5 对比：

- Q2 symbol set `5222/5222`，逐字段 value mismatch `0`；规范化语义 SHA
  `44424eee6fd2b0f914b6ab2420a7d52b1d80480beda80ccb56005354a68d55eb` 相同。
- 0920、0924、0925、latest、0925 anchor legacy projection 全部相等。
- 0920、0924、0925、latest A2 projection 全部相等。
- DB0 对 `task009pbarrier23:` 前缀命中 `0`。

Core 在 `observed_at=2026-09-23T09:25:09+08:00`、显式 10 秒 freshness
policy 下直接读 DB14：`status=PARTIAL`、coverage `1.0`、quotes `5222`、
missing `0`、stale `154`；重复读取 projection hash 相同，并对四个真实
symbol 运行 Engine fact-only shadow。DB5 在同一观察时间得到相同 projection
hash 和 stale 数。详细读回证据见 `core_readback.json`。

## 结论

```text
TD_3S_SELECT_BOUNDARY                 PASS (second real date)
ONE_SLICE_INPUT_WITHOUT_SIZE_CHUNK    PASS (validation binary)
IN_SLICE_BARRIER_ORDER                PASS (second real date)
Q2_SEMANTIC_PARITY                    PASS (DB14 vs DB5)
0920_0924_0925_AUCTION_PARITY         PASS (DB14 vs DB5)
PER_SLICE_SOURCE_STATS                 PASS (203 slices, evidence saved)
CORE_REDIS_READBACK                    PASS_WITH_PARTIAL_FRESHNESS
PRODUCTION_REDIS_SIDE_EFFECTS         NONE_OBSERVED
RABBIT_DELIVERY_EQUIVALENCE            UNKNOWN
HISTORICAL_ARRIVAL_ORDER               UNKNOWN
HISTORICAL_AVAILABLE_AT                UNKNOWN
NORMAL_OPENING                         UNPROVEN
M3_1_NORMAL                            BLOCKED
TD_WRITE_HEALTH                        UNPROVEN
```

两个真实日期（2026-09-24、2026-09-23）都验证了“每片一次 SELECT、片内
业务屏障、同一 t1-v2 Q2/竞价输出”的具体假设，但这仍不是 Rabbit delivery
等价通过：当前没有 Rabbit delivery membership、到达顺序、completion
watermark 或 historical `available_at` 捕获。Core 的 `PARTIAL` 是 stale
事实质量，不是用零补齐或停机条件。不得把本实验升级为 NORMAL 或 M3-1
通过。

## 证据文件

```text
/home/exedev/validation/td-rabbit-phase-p-barrier23-run-20260925T013000+0800/
  t1_v2_stdout.txt
  t1_v2_stderr.txt
  per_slice_stats_v1.json
  redis_semantic_comparison.json
  core_readback.json
  phase_p_barrier23_summary.json
  sha256sums_all.txt
```

下一步只能在保留上述证据并完成主线审计后，将 barrier-aware 语义以 replay-only
方式纳入 Core/t1-v2 适配合同；仍不改生产 consumer/ACK/TD writer，不部署，不
宣称真实 Rabbit arrival parity。
