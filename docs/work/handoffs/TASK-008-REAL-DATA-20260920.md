# TASK-008 real-data opening validation handoff

执行时间：2026-09-20 18:35 +08:00  
交易日参数：`2026-09-18`  
运行类型：盘后只读诊断，不是 NORMAL 09:32 验收

## Status

```text
TASK_008_STATUS=PARTIAL_EVIDENCE
NORMAL_OPENING_PASS=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Evidence

目录：

```text
/home/exedev/validation/task008-opening-validation-20260920T183000+0800/
```

输入使用现有 Redis Q2 只读路径，未连接 Rabbit、未写 Redis/TD、未重启
服务、未触发通知或 effect。

真实 Q2 结果：

```text
projection_status=PARTIAL
projection_consistency=BEST_EFFORT_PARTIAL
freshness_status=STALE_OR_MIXED
expected_symbol_count=5224
quote_count=1000
coverage=0.19142419601837674
stale_symbol_count=1000
missing_symbol_count=4224
```

bounded facts：`000001` 有可用字段；`300750`、`600519` 在该 Q2 cohort
中不可用。缺失没有被补成零；`amount_2m_yuan=0.0` 仅保留为源字段事实，
不改变整体 `PARTIAL/STALE_OR_MIXED` 状态。

Engine opening shadow（`000001`）：

```text
processed_signals=2
fact_status=PARTIAL
decision_status=FACT_ONLY
state=OBSERVE
trigger_id=OPENING_0932
```

## Limits

本次 observation 时间为盘后，不能证明 09:32:10 的实时可用性、历史
`available_at` 或 NORMAL opening 等价性。TASK-008 保持 `PARTIAL_EVIDENCE`，
待受控交易窗口或具备可证明 cutoff 的历史证据后再审计收口。
