# TASK-008 real replay follow-up — frozen production Q2 capture

执行时间：2026-09-20 18:57 +08:00
回放类型：冻结的生产 ground-truth capture，Core 只读离线回放
目标交易日/观察时间：`2026-09-18T09:32:10+08:00`

## 结果

```text
TASK_008_REPLAY=REPLAY_PARTIAL
REPLAY_DETERMINISTIC=PASS
NORMAL_OPENING_PASS=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## 输入证据

来源目录（TASK-001 真实 Redis 捕获）：

```text
/home/exedev/validation/replay-20260918-0915-0940-20260920T014707+0800/
```

输入文件：`redis_q2_capture.json`，其 projection 的
`trade_date=2026-09-18`，包含 5,224 行和 5,224 个 symbol。该文件是之前
真实 Redis `SMEMBERS/HGETALL` 只读捕获的冻结输入；本次没有重新读取或写入
Redis/TD/Rabbit。

```text
input_sha256=21e00cc3a7730ffff114dfac531f58f72640198fdd0f622800c81ef02cf32f51
```

运行输出：

```text
/home/exedev/validation/task008-replay-opening-20260918T185727+0800-v2/replay_summary.json
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
expected_count=5224
observed_count=5224
missing_count=0
coverage=1.0
stale_count=5
future_ts_count=5219
projection_status=PARTIAL
consistency=BEST_EFFORT_PARTIAL
replay_status=REPLAY_PARTIAL
```

source timestamps 日期与目标交易日一致，但时间范围为
`2026-09-18T00:00:00+08:00` 至约 `2026-09-18T15:29:30+08:00`。相对
`09:32:10` cutoff，5 条记录已过期，5,219 条记录属于未来时间。Core 因此
保留 `stale`/`future_ts` 错误，未将数据提升为 `READY`，也没有把未来数据
解释成 opening 时刻已经可用。

这证明了回放链路可以消费目标交易日的真实 Redis 捕获，但不能证明这些字段
在 09:32:10 opening 时刻可用，也不能证明 NORMAL 生产等价性。不得把完整
symbol 覆盖误解成时间有效性或历史 `available_at` 证明。

## 审计结论

- 真实输入回放：完成；不是 synthetic fixture。
- 2026-09-15 capture 的先前回放仅为辅助测试，不属于 TASK-008 正式日期证据。
- ordered/shuffled 确定性：通过。
- 缺失与过期语义：保留，未补零、未伪造 READY。
- opening 结果：仅 `FACT_ONLY` / `OBSERVE`，`fact_status=PARTIAL`。
- historical arrival / available_at：`UNKNOWN`。
- M3：`M3_1_NORMAL=BLOCKED`、`TD_WRITE_HEALTH=UNPROVEN` 保持不变。
