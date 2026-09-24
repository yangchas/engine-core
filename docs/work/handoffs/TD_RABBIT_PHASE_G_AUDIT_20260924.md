# TD/Rabbit global replay Phase G real Redis write audit

审计时间：2026-09-24 20:02–20:04 +08:00<br>
交易日：2026-09-18<br>
源窗口：`[09:20:00,09:25:09)` Asia/Shanghai

## 结论

```text
REAL_TD_TO_T1V2=PASS
ISOLATED_REDIS_WRITE=PASS
REDIS_REPEAT_SEMANTIC_DETERMINISM=PASS
CORE_READS_REAL_REDIS=PASS
SNAPSHOT_FIELD_PARITY=PARTIAL
PHASE_G_OVERALL=PARTIAL
```

## 真实链路

使用当前发布版 `/home/exedev/services/t1-v2/current/content/bin/t1_v2`，直接从
TD `market_data1.stock_tick_v2` 读取，没有 `q2frame` 替代输入：

```text
source_in=129281
source_reject=0
batches=305
ticks=129281
clocks=1
redis_cmds=260710
redis_committed=129281
td_sql=0
ack=0
```

回放写入 Redis DB15，前缀为 `task009g:`；第二次使用 DB10、前缀
`task009g2:`。两次均生成 `0920`、`0924`、`0925`、`latest`、anchor、runtime
和 5209 个逐股 Q2 hash。A2 元数据实际为 `0920 ts=09:20:03,n=1252`、
`0924 ts=09:24:10,n=5038`、`0925 ts=09:25:06,n=5209`；`latest` 的源时间为
09:25:04。去除隔离前缀并排除只反映前缀长度的
`runtime.redis_bytes` 后，5220 个语义 key 全部一致，规范化 SHA-256 为：

```text
89af33ea3c469271ca419737e36edb091f5af4030cca19af16fc6055e502add8
```

## Core 读取

从 DB15 读取 5209 个真实 Q2 hash，交给 Core opening validation：

```text
ordered/shuffled projection hash equal=true
row_count=5209
replay_status=REPLAY_PARTIAL
normal_opening_pass=UNPROVEN
production_side_effects=NONE_OBSERVED
```

`REPLAY_PARTIAL` 是因为该离线 capture 的时间字段不能证明历史
`available_at`；不是 Redis 写入或确定性失败。

## 隔离与副作用

- DB0 回放前后均为 12269 keys，未发现 `task009g*` 前缀。
- TD 写入关闭，`td_sql=0`。
- Rabbit/ACK 未访问，`ack=0`。
- 未重启 `engine-next` 或 `t1-v2-live`。
- DB15 在测试前已有其他隔离数据；本次只使用唯一 `task009g:` 前缀，未清理或覆盖其他 key。

## 未关闭项

与 Phase F 相同，`auction_snapshot_v2` 的 amount/rest 字段与 Q2 的同口径
历史生产证明仍未完成；Rabbit delivery membership/arrival、09:25:06 实盘可见
集合和 historical `available_at` 仍为 `UNKNOWN/UNPROVEN`。因此不能把本阶段
升级为完整快照 parity，也不能把 TASK-008 标为 NORMAL 或放宽 M3-1。

证据目录：

```text
/home/exedev/validation/td-rabbit-phase-g-0920-0925-20260924T200214940+0800/
```

主要文件：`t1_v2_stdout.txt`、`redis_before.json`、`redis_after.json`、
`redis_repeat_comparison.json`、`redis_q2_capture.json`、
`core_opening_from_real_redis.json`、`sha256sums.txt`。

## 后续真实 TD 源时刻核对

为解释 Phase F 中的 amount/rest 差异，另做了一次只读逐股核对：对
`09:20:00–09:25:06.197` 的 129281 条真实 TD tick，按当前发布版
`AuctionCalculator` 逐条计算 `match/br/ar`，再与 0925 `auction_snapshot_v2`
对照。结果为：

```text
0925 snapshot rows                 5221
无 barrier 前 TD tick 的股票          12
与某一条历史 TD tick 公式完全一致      5184
与 barrier 前最后一条 tick 完全一致     3576
```

完全匹配的历史源时刻主要集中在 `09:25:00`（3606 行）、`09:24:59`
（693 行）、`09:25:01`（681 行）和 `09:25:02`（121 行）；匹配源时刻与
快照写入时间的中位差约 6197ms，最大约 303197ms。

这说明旧快照与某个真实历史 tick 状态高度对应，但不证明该 tick 在
09:25:06 当时已经通过 Rabbit 到达，也不证明它就是实时可见集合的完整
顺序。当前最可信的解释从“字段公式不一致”收窄为“快照生成时的可见/到达
集合与 TD 事件时间回放集合不同”，仍保持 `RABBIT_DELIVERY_EQUIVALENCE`
和 `HISTORICAL_AVAILABLE_AT` 为 `UNKNOWN`。不能据此把 replay 的最后事件时间
状态直接替换成旧快照值，也不能把旧快照反向当作 Rabbit arrival 证据。

该核对的完整证据在：

```text
/home/exedev/validation/td-rabbit-phase-h-snapshot-source-20260924T200951431+0800/
```

包含 `report.json`、`exact_match_source_times.json`、`per_symbol.json` 和
`sha256sums.txt`，全程 TD `SELECT`，未写 Redis/TD。
