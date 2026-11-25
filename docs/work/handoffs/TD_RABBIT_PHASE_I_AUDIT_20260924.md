# TD/Rabbit global replay Phase I cross-day Redis validation

审计时间：2026-09-24 20:15–20:18 +08:00<br>
交易日：2026-09-23<br>
源窗口：`[09:24:00,09:25:09)` Asia/Shanghai

## 结论

```text
REAL_TD_TO_T1V2=PASS
ISOLATED_REDIS_WRITE=PASS
REDIS_REPEAT_SEMANTIC_DETERMINISM=PASS
CORE_READS_REAL_REDIS=PASS
SNAPSHOT_SET_PARITY=PARTIAL
PHASE_I_OVERALL=PARTIAL
```

本阶段是对 Phase G 的跨交易日真实复核，不改变 TASK-008 的
`PARTIAL_EVIDENCE`，不把历史回放升级为 NORMAL，也不改变
`M3_1_NORMAL=BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`。

## 真实链路

使用当前发布包：

```text
/home/exedev/services/t1-v2/current/content/bin/t1_v2
```

输入为 TD `market_data1.stock_tick_v2`，回放参数为：

```text
DATA_SOURCE=tdengine_replay
2026-09-23 09:24:00 <= ts < 09:25:09
REDIS DB9 / prefix task009i:
REPLAY_WRITE_REDIS=true
REPLAY_WRITE_TDENGINE=false
```

运行输出：

```text
source_in=44276
source_reject=0
batches=64
ticks=44276
clocks=1
redis_cmds=89010
redis_committed=44276
td_sql=0
ack=0
```

回放生成 09:24、09:25、latest、anchor 和 runtime 投影；Redis DB9
前缀 key 数为 5215。t1-v2 没有使用 q2frame 替代输入，Q2 由真实 TD
tick 经过当前发布版 t1-v2 计算。

## 独立重复

同一窗口再次使用 Redis DB8 / `task009i2:` 执行，运行指标相同。规范化
前缀并移除仅反映前缀长度的 `runtime.redis_bytes` 后：

```text
db1_keys=5215
db2_keys=5215
missing=[]
extra=[]
value_mismatch_count=0
normalized_sha_1=247acea20dddac6f29e9c3278c168b16bba69bd953b9e56f5ea05d5d1d606003
normalized_sha_2=247acea20dddac6f29e9c3278c168b16bba69bd953b9e56f5ea05d5d1d606003
semantic_equal=true
```

## Core 真实 Redis 读回

从 DB9 的 5206 个逐股 Q2 hash 生成冻结 capture，再交给 Core
`run_task008_replay_opening_validation.py`：

```text
row_count=5206
coverage=1.0
ordered/shuffled projection hash equal=true
sample engine hashes equal=true (000001,000002,688622,689009)
replay_status=REPLAY_PARTIAL
normal_opening_pass=UNPROVEN
```

`REPLAY_PARTIAL` 的原因是 capture 不能证明历史 `available_at`；这不是
Redis 写入失败。Core 读取的是冻结文件，不再次连接 Redis/TD/Rabbit，也不
产生副作用。

## 真实数据差异（不作猜测）

对同日 TD 与 `auction_snapshot_v2` 做集合核对：

```text
TD 源 tick 行数（窗口）       44276
TD 源股票数                  5206
t1-v2 Q2 股票数              5206
0925 快照行数/股票数         5222
快照中窗口没有源 tick         16
```

缺少源 tick 的 16 个 symbol：

```text
000016 002691 002731 002853 002860 002868 002877 300082
300585 301139 601059 601198 601238 603400 605303 688496
```

这只证明“该 TD 窗口返回集合”和“0925 旧快照集合”不同，不能证明 t1-v2
错误，也不能证明 Rabbit 到达集合。旧快照时间为
`2026-09-23 09:25:06.268+08:00`；该时间是快照写入时间，不是 Rabbit
arrival 或每只股票的历史 `available_at`。

## 副作用边界

- Redis 只写 DB9/DB8 的唯一测试前缀；DB0 当前 `12269` keys，未发现
  `task009i*` 前缀。
- TD 写入关闭，日志 `td_sql=0`。
- Rabbit/ACK 未访问，日志 `ack=0`。
- 未重启 `engine-next` 或 `t1-v2-live`。
- 未修改生产目录、consumer、ACK、TD retention 或生产 Redis key。

## 未关闭项

```text
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
SNAPSHOT_FIELD_PARITY=PARTIAL
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
```

不能用 2026-09-23 的集合差异反向选择 TD tick，也不能用旧快照补造
缺失 tick。下一步仍应在同一 t1-v2 版本下，分别核对候选盘口、Q2 派生
字段和冻结锚点的同口径字段；若缺少历史 Rabbit 证据，结论保持
`PARTIAL/UNPROVEN`。

## 证据目录

```text
/home/exedev/validation/td-rabbit-phase-i-0923-0924-0925-20260924T201547736+0800/
/home/exedev/validation/td-rabbit-phase-i-0923-0924-0925-20260924T201754287+0800-repeat/
```

主要文件：`t1_v2_stdout.txt`、`redis_before.json`、`redis_after.json`、
`redis_repeat_comparison.json`、`redis_q2_capture.json`、
`core_opening_from_real_redis.json`、`symbol_set_audit.json`、
`td_snapshot_summary.json`、`sha256sums.txt`。
