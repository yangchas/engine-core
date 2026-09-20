# TASK-007 performance optimization handoff

审计时间：2026-09-20 17:29–18:04 +08:00  
任务：TASK-007 Offline canonical replay / auction facts  
分支：`codex/feature-session-engine-integration`

## 结论

```text
TASK_007_REAL_DATA_FUNCTIONAL=PASS
TASK_007_REAL_DATA_DETERMINISM=PASS (此前 FULL ordered/shuffled evidence)
TASK_007_PERFORMANCE=OPTIMIZATION_REQUIRED (not a functional failure)
TASK_007_ACCEPTANCE=仍待独立 auditor 收口
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

耗时只作为性能优化指标，不作为真实数据回放正确性的失败条件。

## 本次无语义变化的优化

- `FINAL` verification path 复用增量横截面 state/Q2 projection；最终 frame
  仍生成完整 state hash 用于 parity。
- canonical value hash 延迟到实际访问时计算；semantic hash 合同不变。
- canonical encoder 使用单一 `bytearray`，保持 `CanonicalHashEncodingV1`
  字节结果不变。
- TD 热路径缓存交易日、symbol normalization、market fallback 和不可变
  `FieldMetaV1`；TD scalar aliases/array aliases 改为模块级常量。
- TD 真实 runner 直接把 timestamp 转为 epoch milliseconds，避免字符串往返。
- 增量 replay 已经生成 `latest_raw` 后，跳过同一 frame 的第二次
  `event.to_q2_raw()` 转换。

以上改动不连接 Rabbit/Redis/TD 写入，不改变 ACK、producer、systemd 或 effect。

## 真实 500-frame FINAL 结果

验证目录：
`/home/exedev/validation/task007-perf-final-500-hotpath-20260920T172951+0800/`

```text
window                 2026-09-18 09:15:00–09:40:00 Asia/Shanghai
frames                 500
stock rows             1,224,811
expected symbols       5,221
empty frames           98
processed_signals      500
reducer_revision       500
final VirtualClock     2026-09-18 09:40:00 Asia/Shanghai
elapsed                811,534.946 ms (13.53 min)
status                 REPLAY_READY_BOUNDED
```

与此前同一 `FINAL` verification level 的 16.75 分钟结果相比约快 19.2%；
与最初 18.07 分钟 FULL real ordered 结果相比约快 25.2%。最终
`session_content_hash`、`session_evidence_hash`、`signal_hash` 与此前 FINAL
基准一致：

```text
session_content_hash = b022943ea7189136ad7ad7be4b21d838eb98071dec3f2ba3475732776de8bdf6
session_evidence_hash = d78897a4d5aa4f028d0ee9e6c630d8b0666ce4c5364a64eb238b824df0cbc471
signal_hash = cc7101d1db8fb84afcb1bc60d7474463b8d7ea164ee3b729b80cbc9903dd7bae
```

旧的 FULL ordered/shuffled 500-frame validation 仍是 determinism 主证据；本次
优化 run 是 ordered FINAL 性能和最终 hash parity 证据，不能把 FINAL 的中间
frame hash 与 FULL 中间 frame hash 混为同一合同。

## 性能实验记录

```text
FULL 20-frame baseline                         39,958.350 ms
incremental FINAL 20-frame                    38,484.339 ms
lazy value hash 20-frame                      36,758.523 ms
string cache variant 20-frame                 36,910.670 ms  (rejected)
alias/date/metadata hot-path 20-frame         33,330.603 ms  (best measured)
old FULL 500-frame real ordered             1,084,378.912 ms
prior FINAL 500-frame optimization           1,004,886.319 ms
current FINAL 500-frame hot path               811,534.946 ms
```

`string cache variant` 无收益，未保留。当前仍超过历史 `<=5 min` 目标，因而
性能状态保持 `OPTIMIZATION_REQUIRED`；这不改变 TASK-007 的真实功能 PASS。

## 副作用审计

本次 runner 仅执行 TD `DESCRIBE`/`SELECT`：

```text
td_write=False
redis_write=False
rabbit_consume=False
rabbit_ack_change=False
service_restart=False
effect_or_notification=False
production_directory_write=False
```

详见验证目录 `side_effect_audit.json` 和 `sha256sums.txt`。

## 后续

TASK-007 仍需独立 auditor 对本次 diff 和两类真实证据做最终审查后，才可将
任务板状态改为 `MERGED`。性能继续作为后续优化项；TASK-008 不自动启动。
