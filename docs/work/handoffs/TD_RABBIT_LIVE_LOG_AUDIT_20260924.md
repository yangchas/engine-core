# t1-v2 live log arrival-evidence audit

审计时间：2026-09-24（Asia/Shanghai）<br>
范围：只读检查 Cobra 当前 `t1-v2` 运行日志；不消费 Rabbit、不写 Redis/TD、不重启服务。

## 观察到的真实运行字段

`/home/exedev/t1v2work/logs/t1_v2.log` 在 09:20–09:26 的 progress 行包含：

```text
wall time
batches
source_in
source_reject
ack / ack_fail
ticks
redis_cmds / redis_committed
last_ts_ms
wall_lag_ms
```

代表性行（字段值原样来自日志，未包含认证信息）：

```text
09:20:00  batches=661  source_in=224676  ack=661  last_ts_ms=09:20:00
09:24:51  batches=1277 source_in=446039  ack=1277 last_ts_ms=09:24:50
09:25:01  batches=1303 source_in=463588  ack=1303 last_ts_ms=09:25:00
09:26:01  batches=1317 source_in=472958  ack=1317 last_ts_ms=09:26:00
```

## 证据边界

日志中未发现以下字段：

```text
Rabbit batch_id
Rabbit sent_at per delivery
delivery_tag 与 tick 的关联
每条 tick arrival time
delivery sequence
completion / watermark
0925 snapshot batch membership
```

因此本次真实日志只能支持：

```text
LIVE_PROGRESS_COUNTERS=OBSERVED
ACK_COUNTERS=OBSERVED
RABBIT_DELIVERY_MEMBERSHIP=UNKNOWN
RABBIT_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
```

09:25:01 到 09:26:01 之间没有更细的 progress 行，不应解释为没有
消息或没有 09:25:06 处理；progress 间隔本身不是 completion watermark。

## 结论

真实生产日志没有提供足够信息证明 Rabbit delivery 与 TD event-time
回放等价，也不能证明 09:25:06 屏障前的最终可见集合。继续保持
`PHASE_L_PARTIAL`、`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`，不新增 consumer
或修改 ACK 以补造缺失证据。

## 同日 Redis/TD 只读对照（2026-09-24）

随后对当前生产 Redis DB0 和 TD 做了只读集合核对：

```text
Redis q2:active:20260924       5225 symbols
Redis q2:<symbol> hashes       5225
TD stock_tick_v2 09:15–09:25:09 5222 symbols
TD stock_tick_v2 09:15–09:40   5222 symbols
TD auction_snapshot_v2 0925    5222 symbols
```

Redis active 集合比同日 TD/0925 快照多出 3 个 symbol：`001246`、`301660`、
`301716`。这 3 个 hash 的 `ts` 都是交易日零点，且 `px/pc/amt/vol/iv/ia/ln`
等字段为零；TD 在当日 09:15–15:04 没有对应 tick。它们应标为
`STALE/UNAVAILABLE` 的占位状态，不能当作有效行情，也不能把 active set
数量直接当成实时全市场 coverage。该事实不修改生产 Redis，只记录为源集合
差异。

0920/0924/0925 生产 Redis 元数据的实际 `n` 为 `4865/5116/5208`，而
0925 TD 快照为 5222 行；这再次说明 A2 Top/候选集合与全市场 Q2/TD 集合
不是同一个 coverage 合同，不能用 `n=5208` 宣称全市场冻结完成。

用 Core 现有只读 `RedisQ2ProjectionAdapter` 读取当前 DB0（观察时刻为
审计时刻、freshness policy=10 秒）得到：

```text
expected=5225 observed=5225 missing=0 coverage=1.0
status=STALE consistency=BEST_EFFORT_STALE stale=5225
```

上述 3 个 Redis-only symbol 均保留为 quote，但各自带有 `stale` 错误；
这证明当前 Core 不把它们静默改成零缺失，也证明 coverage=1.0 不能替代
freshness/可用性判断。
