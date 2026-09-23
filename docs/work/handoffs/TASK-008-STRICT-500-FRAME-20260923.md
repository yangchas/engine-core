# TASK-008 strict 500-frame Q2Frame handoff

日期：2026-09-23（Asia/Shanghai）  
状态：`STRICT_Q2FRAME_PASS`; `NORMAL_OPENING=UNPROVEN`

真实 t1-v2 以 TD 只读方式读取 2026-09-18 09:15–09:40 的每个 3 秒半开区间，
将同一 slice 的多个 source chunk 在本地合并为单一 Q2Frame，并保留空 slice。
结果为 500 个连续 frame、98 个空 frame；`logical_ts_ms` 使用 slice end，
最后到 09:40:00。Redis、TD 写入和 Rabbit consume/ACK 均为 0。

Core 对该 gzip artifact 做 ordered/repeat 两次回放，结果：

```text
REPLAY_READY_BOUNDED
deterministic=true
frame_count=500
update_count=1200468
symbol_count=5221
q2_coverage=1.0
processed_signals=500
reducer_revision=500
virtual_clock=2026-09-18T01:40:00Z
final_state_hash=9b8406d57f93e3e1dc6c5d43c230f9497462b374ab6d43a08c674eee96167e11
```

完整证据与 SHA 位于：
`/home/exedev/validation/task008-q2frame-real-20260923/`。

这只关闭 Q2Frame bridge/strict frame evidence，不替代 09:32:10 NORMAL opening
可用性或历史 `available_at` 证明；M3-1 仍为 `BLOCKED`，
`TD_WRITE_HEALTH=UNPROVEN`。
