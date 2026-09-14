# Legacy engine-next 与 engine_core 只读读取链探查 — 2026-09-14

## 范围

本次只读探查比较两个独立入口的观测边界：

```text
engine-next release
  → IntradayContextBuilder
  → build_auction_plate_bucket_stats

engine_core
  → RedisQ2ProjectionAdapter
  → DeterministicEngine + ProbeStrategy
```

这不是策略迁移、报告等价或历史回放验证。没有修改 Redis/TD，没有新增
Rabbit consumer，没有 ACK、通知、恢复或 effect。

## 运行身份

```text
host: cobra-ion
legacy release: /home/exedev/services/engine-next/releases/20260903_e272842
core archive: /home/exedev/validation/engine-core-244e7ae/extract
core commit: 244e7ae
python: 3.12.3 (shared engine-next runtime)
trade_date: 2026-09-14
symbols: 000001, 300750, 600519
```

## Legacy context probe

命令通过 `GuardRedis` 保护旧读取路径，仅允许已分类的 Redis 读命令；缓存写入、网络
fallback、恢复、通知和 effect hook 均被禁用。

```text
contract: EngineNextContextProbeV1
phase: auction
snapshot_count: 3
guard_writes: []
read_only: true
artifact: /home/exedev/validation/engine-core-244e7ae/context-probe-20260914.json
artifact_sha256: b089628a167f8ac736407ea1b0160c77dabc46226208b1adc0858aac1e84904b
```

旧上下文和旧纯事实函数确实读到了三只标的，均返回 `expectation=observe`。但该 probe
使用旧链的当前 Redis 状态来诊断一个指定的 09:26 时间，结果显示：

```text
probe_now_ms: 1789349160000
latest_quote_timestamp_ms: 1789369202000
future_source_timestamp: true
legacy_future_timestamp_handling: CLAMPED_TO_ZERO_AGE
```

这说明旧 context builder 的实时读取入口不能直接作为历史回放 oracle：它会读取当前
cohort 中晚于诊断时间的源记录，并把未来年龄裁为零。该行为只作为 legacy observed
evidence 记录，不能复制到 Core 的历史时间门禁。

## engine_core Q2 probe

Core probe 使用相同 Cobra Redis 端点的只读 `SMEMBERS/HGETALL`，并将观察结果送入同一
`DeterministicEngine` 两次：

```text
requested_count: 5220
quote_count: 5220
row_coverage: 1.0
status: STALE
consistency: BEST_EFFORT_STALE
same_observation_engine_deterministic: true
read_operation_counts: smembers=1, hgetall=5220
guarded writes: none
artifact: /home/exedev/validation/engine-core-244e7ae/q2-probe-20260914.json
artifact_sha256: b7e2538bb0edfbb314509714f36ab31aaef475860079af34a5254ec1868124bd
```

`coverage=1.0` 仅表示 5220 个 active-set symbol 都读到；当时源数据已不满足
freshness policy，因此 Core 保持 `STALE`，没有把 coverage 自动升级为 `READY`。

## 跨链路结论

```text
legacy context read-only safety: PASS
core Q2 read-only safety: PASS
core repeated observation determinism: PASS
legacy context ↔ core exact input parity: UNPROVEN
historical replay safety of legacy context: NOT_ALLOWED
```

两条 probe 没有共享同一个不可变输入快照，也没有统一的 source-time as-of 对齐；因此
不能根据三只股票数值看起来相近就填写 `MATCH` 或 `FIRST_DIVERGENCE`。当前正确状态是
`UNPROVEN`。后续只有在获得同一 captured Q2 内容、同一观察时间和字段 authority 后，
才进行跨链路逐字段比较。

## 后续边界

1. 旧 `engine_next` 继续作为生产主链，不改其未来时间裁剪行为。
2. Core 继续只读 Shadow；`source_record_time_ms` 仅用于 freshness、future-skew 和
   source-time range，不解释为 Rabbit arrival 或交易所逐笔顺序。
3. 不为这次差异新增通用 Provider、Replay、watermark 或兼容层。
4. 第一条 Auction Shadow 策略仍需真实 consumer oracle、状态生命周期和同输入
   differential；本探查本身不授权策略迁移。
