# TASK-007 read-only production-boundary audit

审计时间：2026-09-20 18:08 +08:00  
审计对象：TASK-007 当前开发分支、真实回放 evidence、性能优化变更

## 结论

```text
LOCAL_AUDIT=PASS
REAL_DATA_FUNCTIONAL=PASS
REAL_DATA_DETERMINISM=PASS (earlier FULL ordered/shuffled evidence)
OPTIMIZED_FINAL_HASH_PARITY=PASS
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
PERFORMANCE_TARGET=OPTIMIZATION_REQUIRED
TASK_007_FORMAL_STATE=RUNNING_PENDING_INDEPENDENT_AUDITOR
```

本报告是本地只读证据审计，不冒充任务板要求的独立 auditor 签核；因此不把
TASK-007 自动改为 `MERGED`。

## Evidence checked

- `/home/exedev/validation/task007-real-ordered-20260920T154654+0800/`
- `/home/exedev/validation/task007-real-shuffled-20260920T161558+0800/`
- `/home/exedev/validation/task007-perf-final-500-hotpath-20260920T172951+0800/`
- `/home/exedev/validation/task007-perf-compat-20260920T180801+0800/`
- `sha256sum -c sha256sums.txt`：上述 evidence 均通过
- 当前 HEAD `16a0573` 之前的性能提交与本次兼容性修复的工作树差异

## Real-data checks

```text
500 frames
1,224,811 stock rows
5,221 expected symbols
98 empty frames retained
500 signals / reducer revision 500
VirtualClock = 2026-09-18 09:40:00 Asia/Shanghai
optimized FINAL session_content_hash = prior FINAL baseline
optimized FINAL signal_hash = prior FINAL baseline
```

此前 FULL ordered/shuffled 500-frame comparison 为 `PASS=True`；shuffle 只改变
frame 内 TD 行顺序，不证明 Rabbit 历史 arrival order。

## Boundary checks

- `src/engine_core` 未发现 `taos`、Rabbit client、Redis client 或 writer import。
- 真实 runner 只执行 TD `DESCRIBE`/`SELECT`；没有 Redis/Rabbit consume、ACK
  改动、TD/Redis 写入、服务重启或 effect。
- `source_sequence`、Rabbit arrival order、historical `available_at` 仍是
  `UNKNOWN`，没有被优化过程升级为已知事实。
- `MarketTickV1` 的公开 hash 字段保持原 dataclass 序列化名称；新增回归测试
  防止 lazy cache 泄露为私有合同字段。
- M3-1 生产门禁仍为 `M3_1_NORMAL=BLOCKED`、`TD_WRITE_HEALTH=UNPROVEN`。

## Missing sign-off

TASK-007 仍缺项目定义的独立 auditor 最终签核。完成该签核前，不应把任务板
状态改为 `MERGED`，也不应启动 TASK-008 或生产 NORMAL shadow。
