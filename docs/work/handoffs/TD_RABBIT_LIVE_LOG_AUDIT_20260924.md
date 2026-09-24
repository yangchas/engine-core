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

## 补充定量核对：progress 累计计数与 batch 大小采样

2026-09-25 对该日志文件的 2026-09-24 09:15–09:40 区间做了只筛选/聚合，
未输出认证配置或无关日志：

```text
progress rows:                  109
first progress:                 09:15:01 batches=52 source_in=45,378 ack=52
last progress:                  09:39:58 batches=3,348 source_in=1,975,895 ack=3,348
counter delta:                  3,296 processed batches / 3,296 ACKs
counter delta source records:   1,930,517
records per processed batch:    585.7 (counter-delta weighted mean)
sampled last_in:                min=16 max=1,000 mean=497.7; 23/109 exactly 1,000
```

解释范围：运行日志路径来自 `t1-v2-live` 的 `LOG_FILE_PATH`，服务配置为
`DATA_SOURCE=live`；运行代码把每次成功处理的 source batch 计入 `batches`，
把 decoder 交给 `SourceTickBatchBuilder` 的记录数计入 `source_in`，处理后再
ACK。首末 progress 计数之差是这两条进度记录之间已处理并确认批次/记录的
聚合量。`last_in` 只表示每条进度日志前最后一个 batch 的记录数，是稀疏
样本，不是完整 batch-size histogram；样本最大 1,000 也不能证明协议硬上限为
1,000。

Release reader 的源码合同是：一次 `RabbitMqTickSource::next_batch()` 调用只做
一次 `amqp_consume_message`，随后只解析这一条 envelope 中的一个
`DataRequest/DataBatch`，逐条转为 `SourceTickRecord`，不在 consumer 内等待或
合并后续 message。内部 `TickBatch` 保存 ticks、logical/wall time 和 session
sequence，但当前 decoder 没把 `DataBatch.batch_id/sent_at` 传入它；因此 progress
计数不能还原 batch 与 frame 的成员映射，也没有所有 symbol 到齐的 marker。

此窗口首末 progress 相隔约 1,497 秒，聚合批次数相当于约 6.6 个已处理 batch/
名义 3 秒；这只能作为速率观察，**不能**把 batch 按比例映射到任何具体 TD
frame。它与“每个 3 秒时间片固定恰有 3–4 个 delivery”的简单解释不一致；但
由于日志没有逐条 delivery 的 `batch_id`、records 成员、tick `tss` 范围或完成
标记，不能据此否定用户提供的分批描述，也不能证明每条 message 的事件时间范围。

运行服务当时指向 release `20260923_tdstop0945b`；活动 executable SHA-256 为
`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`。Release
目录内源码可说明 reader 的处理合同，但没有可核对的 build manifest 将 binary
精确绑定到某次源码提交，故源码/二进制构建同一性仍为 `UNPROVEN`。本地和当前
release 搜索未找到可确认的上游 market publisher 实现；旧 Node receiver 未观察到
其 gRPC 端口 listener，不能拿它代表当前 publisher。

因此更新为：

```text
LIVE_BATCH_COUNTERS=OBSERVED
LIVE_BATCH_MEMBERSHIP=UNKNOWN
LIVE_BATCH_TO_3S_FRAME_MAPPING=UNKNOWN
USER_REPORTED_3_TO_4_DELIVERIES_PER_SLICE=UNVERIFIED_BY_RUNTIME_CAPTURE
PUBLISHER_SOURCE_PROVENANCE=UNKNOWN
```

补齐 membership 的最小证据应来自现有 publisher 代码或其已存在的、不含 payload
和凭据的逐 delivery 记录（`batch_id`、`record_count`、`sent_at`、tick 时间
min/max）；不通过新增 Rabbit consumer、peek/requeue 或改变 ACK 获取。

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
