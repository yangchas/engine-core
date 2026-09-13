# 当前 Core 验证证据（2026-09-14）

## 验证身份

- 仓库：`engine_core`
- 分支：`codex/feature-session-engine-integration`
- 验证 commit：`800487427f28eb58ad7e8f30a2256ed95d7c3dee`
- 本次代码变更：`DataRequest` 在契约边界复制并冻结 `symbols`、`required_fields`，拒绝无序集合；未修改生产链、Engine 运行逻辑或数据源。
- 工作树：验证开始前干净。

## 本地验证

```text
python -m pytest -q -p no:cacheprovider
307 passed in 1.13s

python -m compileall -q src tests
PASS

git diff --check
PASS
```

## Cobra-ion 同提交验证

使用当前仓库归档上传到 Cobra-ion 独立 `/tmp` 目录，在生产共享 Python 3.12.3 环境执行；未安装包、未修改 release、未触碰生产数据。

```text
archive sha256 = A954460455E09203C327DD65C68BE24E70158861C1617FD3D15D0A3EEBC886F1
pytest         = 307 passed in 1.65s
compileall     = PASS
verify_rc      = 0
```

该结果证明跨 Windows/Cobra 的离线合同测试和解释器兼容性，不证明生产链已被 Core 接管。

## Cobra 真实 Redis 只读探针

时间：`2026-09-13T23:59:12+08` 左右，目标交易日 `2026-09-14`（开盘前）。

```text
status                         = MISSING
consistency                    = EMPTY_UNIVERSE
requested_count               = 0
quote_count                   = 0
row_coverage                  = 0.0
oldest/newest_source_time_ms  = null / null
read_operation_counts         = {"smembers": 2}
same_observation_engine_deterministic = true
```

探针实际使用 Cobra Redis 连接，仅暴露 `SMEMBERS/HGETALL`；本次没有 HGETALL，因为 active cohort 为空。输出 artifact SHA-256：

```text
b114f8fb8c7528e8ebc764ee685c51876a4f994c60e4332ffa2fbdf04f0a93bf
```

这是一次真实连接的开盘前缺失证据，证明 `EMPTY_UNIVERSE` 会 fail-closed，不能解释成实时行情通过。

## 服务器状态

```text
engine-next  active  MainPID=3181295  NRestarts=0
t1-v2-live   active  MainPID=2878024  NRestarts=0
/dev/root    19G total / 15G used / 3.1G free / 83%
```

2026-09-14 交易日 ground-truth capture 进程 `PID=4014185` 仍在运行，当前目录只有 `capture_started.json`；尚未到 09:20/09:24/09:25 采集点。

## 结论与边界

```text
local_contract_suite                 PASS
cobra_same_commit_suite             PASS
real_redis_connection                PASS (negative pre-open result)
real_live_q2_positive_coverage       NOT_YET_AVAILABLE
0920/0924/0925 current-day capture   NOT_YET_AVAILABLE
engine_next replacement              NOT_READY
```

默认 pytest 仍是离线合同/fixture 套件；`test_real_*` 文件中的 client/provider 为 fake 或 override。真实 Redis、TD 和第三方连接验证必须通过独立 Cobra 只读探针记录，不能把 `307 passed` 解释成生产验收。

已知阻塞：`DeterministicEngine` 的 `_registered_evaluation_ids` 是 session 内无界集合，当前测试明确验证其不会被淘汰；这保证一次性注册，但尚不满足无限期长会话的有界内存要求。`signal_id` 去重也只有有限内存窗口。两者在正式替代 `engine_next` 前必须有明确的 session 生命周期或持久化方案；本证据不宣称已解决。
