# TD/Rabbit global replay Phase L full-window Redis validation

审计时间：2026-09-24 20:39–20:45 +08:00<br>
交易日：2026-09-23<br>
源窗口：`[09:15:00,09:40:00)` Asia/Shanghai

## 结论

```text
REAL_FULL_TD_TO_T1V2=PASS
ISOLATED_REDIS_WRITE=PASS
FULL_WINDOW_REPEAT_SEMANTIC_DETERMINISM=PASS
FROZEN_AUCTION_ANCHOR_NOT_OVERWRITTEN=PASS
FULL_WINDOW_CORE_BRIDGE=PASS
PHASE_L_OVERALL=PARTIAL
```

本阶段首次把 09:15 基线到 09:40 的完整真实 TD 窗口直接送入当前
t1-v2，并允许其写入隔离 Redis；没有使用 q2frame，也没有写 TD 或消费
Rabbit。性能只记录实际耗时，不作为正确性失败门禁。

## 真实链路

发布包：

```text
/home/exedev/services/t1-v2/current/content/bin/t1_v2
```

运行参数：

```text
DATA_SOURCE=tdengine_replay
2026-09-23 09:15:00 <= ts < 09:40:00
Redis DB3 / prefix task009l:
REPLAY_WRITE_REDIS=true
REPLAY_WRITE_TDENGINE=false
```

TD 独立 `SELECT COUNT(*)` 为 `1204178`，与 t1-v2 总结一致：

```text
source_in=1204178
source_reject=0
batches=1224
ticks=1204178
clocks=78
redis_cmds=2430075
td_sql=0
ack=0
```

Redis DB3 产生 5233 个 `task009l:` 前缀 key，包含 5222 个逐股 Q2、
0920/0924/0925/latest A2、anchor 和 runtime。全天 09:25 之后的行情
继续更新滚动 Q2，但 0920/0924/0925 冻结键独立保留。

## 完整窗口重复

同一输入再次写入 Redis DB2、前缀 `task009l2:`。两次均为 1224 批、
1204178 ticks、78 clocks 和 2430075 Redis commands；规范化前缀并排除
`runtime.redis_bytes` 后：

```text
db1_keys=5233
db2_keys=5233
missing=[]
extra=[]
value_mismatch_count=0
normalized_sha_1=d58eb34417ab914548599e938ca9e5834be224d2a41dccabd4ed4bbc12420472
normalized_sha_2=d58eb34417ab914548599e938ca9e5834be224d2a41dccabd4ed4bbc12420472
semantic_equal=true
```

## 冻结锚点不被全天滚动覆盖

将完整 09:15–09:40 运行的以下冻结键与只运行到 09:25:09 的真实
DB5 结果比较：

```text
a2:0920
a2:0924
a2:0925
market:auction:0925
market:auction:anchor
```

五个键全部相同，规范化 frozen SHA 为：

```text
3f5ba38bc3978e125258442b7d31af6cd6bed8b3a48f5c042bac2af251333a17
```

这证明当前 replay 写入路径中，09:25 冻结输出没有被 09:30–09:40
滚动行情覆盖；滚动 Q2 和冻结锚点已分离。

## 副作用边界

- Redis 只写 DB3/DB2 的唯一测试前缀；DB0 未出现 `task009l*` 前缀。
- TD 写入关闭，两个运行的 `td_sql=0`。
- Rabbit/ACK 未访问，两个运行的 `ack=0`。
- 未重启 `engine-next` 或 `t1-v2-live`，未修改生产目录、consumer、
  ACK、TD retention。

## 未关闭项

```text
FULL_WINDOW_CORE_BRIDGE=PASS
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
```

全天最终滚动 Q2 capture 已交给 Core 独立 readback：`row_count=5222`、
coverage=1.0，ordered/shuffled projection hash 相同，抽样 Engine hash
相同；Core 报告 `REPLAY_PARTIAL`，其中 68 个基线/低频报价按明确的
10 秒 freshness policy 为 stale。这个结果证明了真实全天 Q2→Core
桥接和确定性，但不证明 Rabbit delivery equivalence、historical
available_at 或 NORMAL opening，因此不把 Phase L 升级为完整生产迁移通过。

## 证据目录

```text
/home/exedev/validation/td-rabbit-phase-l-0923-full-20260924T203942122+0800/
/home/exedev/validation/td-rabbit-phase-l-0923-full-20260924T204239001+0800-repeat/
```

主要文件：`t1_v2_stdout.txt`、`redis_after.json`、
`redis_repeat_comparison.json`、`frozen_anchor_repeat_comparison.json`、
`final_q2_capture.json`、`core_final_q2_readback.json`、
`a2_runtime_summary.json`、`redis_db0_current.txt`、`sha256sums.txt`。

## 独立复核（2026-09-24 追加）

在不重跑生产路径的前提下，复核了当前运行环境与证据完整性：

```text
t1-v2/current -> releases/20260923_tdstop0945b
t1_v2 binary sha256 = 363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56
engine-next = active
t1-v2-live = active
root available = 22G
DB0 task009l* keys = 0
DB3 task009l* keys = 5233
DB2 task009l* keys = 5233
```

主运行目录和 repeat 目录分别执行 `sha256sum -c sha256sums.txt`，全部
`OK`；Core 全量测试为 `691 passed`，compileall 与 `git diff --check`
通过。以上复核没有写 Redis、TD 或生产文件，也没有重启服务。

当前发布包额外执行 `t1_v2 --self-test` 返回 `t1_v2 self-test passed`；
Core canonical Python 包的静态导入审计未发现 `redis`、`taos`、`pika`、
Rabbit/AMQP 或 TD writer 依赖。

另外核对了当前 exact t1-v2 发布包的时间实现：`SnapshotTrigger`、
`PhaseResolver` 和 `AuctionCalculator` 均通过 `ts_ms / 1000` 再计算
`HHMMSS`。因此 `09:25:06.197` 在竞价屏障判断中按 `09:25:06` 处理，
但原始毫秒仍保留在 tick/source evidence 中；这满足“触发按秒截断、证据保留原始时间”，
不把毫秒误当成新的业务时间点。

同时对真实 TD 做了只读时间分布核对（`[09:24:57,09:25:09)`）：

```text
2026-09-18: 10914 rows / 5208 symbols / 8 second buckets / 4930 multi-tick symbols
  last observed event second = 09:25:04
2026-09-23:  9970 rows / 5205 symbols / 7 second buckets / 4762 multi-tick symbols
  last observed event second = 09:25:03
```

这两个真实日期在 09:25:06 附近都可能没有 tick，支持由独立 Clock
推进冻结，而不是等待一条恰好落在 `09:25:06` 的行情；但它仍不能证明
Rabbit 的实际到达集合或 live barrier 可见性。多 tick 股票数量也说明
Q2 不能预先按 symbol 去重，必须让每条 tick 进入 t1-v2，再把候选选择
作为独立竞价视图。
