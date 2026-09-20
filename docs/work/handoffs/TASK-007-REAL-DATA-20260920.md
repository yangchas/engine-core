# TASK-007 real-data replay handoff

审计时间：2026-09-20 15:46–16:04 +08:00  
审计对象：`codex/feature-session-engine-integration`  
数据源：TD `market_data1.stock_tick_v2` 与 `market_data1.auction_snapshot_v2`  
验证目录：`/home/exedev/validation/task007-real-ordered-20260920T154654+0800/`

## 安全边界

本次仅通过服务器 venv 执行 TD `DESCRIBE`/`SELECT`：

- stock tick 每个 3 秒半开区间单独查询：`[start_ms, end_ms)`；
- auction 仅 `SELECT` 0920/0924/0925；
- 未连接 Rabbit 消费路径，未改变 ACK；
- 未写 TD/Redis，未调用 Wencai，未重启服务，未触发 effect；
- `side_effect_audit.json`：`NONE_OBSERVED`。

## 真实 ordered replay

```text
frames                 500
window                 2026-09-18 09:15:00–09:40:00 Asia/Shanghai
stock rows             1,224,811
expected symbols       5,221
empty frames           98
processed_signals      500
reducer_revision       500
final VirtualClock     2026-09-18 09:40:00 Asia/Shanghai
elapsed                1,084,378.912 ms (18.07 min)
```

单一 `OfflineCanonicalReplay` / `ReplaySessionTimeline` / `DeterministicEngine`
贯穿 500 个 frame；auction 0920、0924、0925 均读取到 5,221 行，coverage
为 1.0，状态为 `READY`。98 个空 frame 未被跳过。

## 状态判断

```text
REAL_TD_READ                         PASS
CANONICAL_500_FRAME_ORDERED_REPLAY   PASS (completed)
SESSION_TIMELINE_500_FRAMES          PASS
AUCTION_0920_0924_0925_READ          PASS
PRODUCTION_SIDE_EFFECTS              NONE_OBSERVED
ORDERED_SHUFFLED_DETERMINISM         NOT_RUN
REAL_FULL_MARKET_REPLAY_FUNCTIONAL   PASS
PERFORMANCE_TARGET                   NOT_MET; OPTIMIZATION_REQUIRED
TASK_007_ACCEPTANCE                  PENDING_AUDITOR
```

性能目标采用 `<=5 min PASS`、`5–10 min PASS_WITH_WARN`、`>10 min
OPTIMIZATION_REQUIRED`。本次 18.07 分钟未达到性能目标，但这不否定真实
回放的功能和语义正确性；它只产生后续性能优化工作项。不能以旧 TASK-004
的非 canonical 结果替代本证据。

20-frame 的真实 ordered/shuffled 双 pass 已完成，最终 session、signal 和
auction hashes 一致（`determinism_probe`：
`/home/exedev/validation/task007-real-determinism-probe-20260920T160725+0800/`）。
500-frame shuffled pass 尚未运行，因此不把小窗口结果扩展成完整窗口证明。

## 未证明事项

- 本次只运行 ordered pass，未完成 500-frame shuffled pass，因此不宣称真实
  全窗口 deterministic equality；
- TD `event-time`/规范化回放顺序不等于 Rabbit 历史 arrival order；
- source sequence、Rabbit arrival order、historical `available_at` 均为
  `UNKNOWN`；
- TD 读取结果不证明生产 Rabbit/TD batch 等价性。

详细行数、schema、hash、auction revision、unknowns 与 sha256 清单见验证目录。
