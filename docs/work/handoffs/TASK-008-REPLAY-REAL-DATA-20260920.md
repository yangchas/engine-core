# TASK-008 real replay follow-up — frozen production Q2 capture

执行时间：2026-09-20 18:50 +08:00  
回放类型：冻结的生产 ground-truth capture，Core 只读离线回放  
目标观察时间：`2026-09-15T09:32:10+08:00`

## 结果

```text
TASK_008_REPLAY=REPLAY_PARTIAL
REPLAY_DETERMINISTIC=PASS
NORMAL_OPENING_PASS=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## 输入证据

来源目录：

```text
/home/exedev/validation/production-ground-truth-20260915/
```

输入文件：`q2_093210.jsonl`。capture manifest 将其标记为
`formal_ground_truth=true`、`source=redis:q2:*`、`slot=09:32:10`，文件
包含 5,220 行和 5,220 个 symbol。

```text
input_sha256=ba38a9d66d49c0c07c8d1934e7755f68575a9e0484b84dd65e07f97bf19bd0e0
```

运行输出：

```text
/home/exedev/validation/task008-replay-opening-20260920T185041+0800/replay_summary.json
```

回放 runner 只读取冻结 JSONL，调用 Core 的 `build_q2_projection`、
`MarketStateReducer` 和 `OpeningShadowStrategy`；没有 Redis/TD/Rabbit client
导入，也没有写入任何外部系统。

## Determinism

同一输入分别以原始顺序和稳定随机乱序运行：

```text
projection_content_hash_equal = true
000001 engine_content_hash_equal = true
000002 engine_content_hash_equal = true
000338 engine_content_hash_equal = true
600519 engine_content_hash_equal = true
```

两个 pass 均使用一个 in-memory Engine，每个选定 symbol 提交 2 个 signal
（market update + opening timer），结果为 `FACT_ONLY` / `OBSERVE`。

## 数据质量边界

```text
expected_count=5220
observed_count=5220
missing_count=0
coverage=1.0
stale_count=5220
projection_status=PARTIAL
consistency=BEST_EFFORT_PARTIAL
replay_status=REPLAY_PARTIAL
```

所有 source timestamps 落在 `2026-09-14`（最早
`2026-09-14T00:00:00+08:00`，最晚约 `2026-09-14T15:00:05+08:00`），与目标
交易日 `2026-09-15` 不一致，并且相对 09:32:10 cutoff 已过期。Core 因此为
每个 quote 保留 `trade_date` 和 `stale` 错误，未将数据提升为 `READY`。

这证明了回放链路可以消费真实生产捕获，但不能证明这些字段在目标 opening
时刻可用，也不能证明 NORMAL 生产等价性。不得把完整 symbol 覆盖误解成时间
有效性或历史 `available_at` 证明。

## 审计结论

- 真实输入回放：完成；不是 synthetic fixture。
- ordered/shuffled 确定性：通过。
- 缺失与过期语义：保留，未补零、未伪造 READY。
- opening 结果：仅 `FACT_ONLY` / `OBSERVE`，`fact_status=PARTIAL`。
- historical arrival / available_at：`UNKNOWN`。
- M3：`M3_1_NORMAL=BLOCKED`、`TD_WRITE_HEALTH=UNPROVEN` 保持不变。

