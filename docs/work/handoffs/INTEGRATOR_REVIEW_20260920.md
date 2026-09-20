# Integrator review — TASK-004/005/006

审计时间：2026-09-20（Asia/Shanghai）  
审计分支：`codex/task-cross-sectional-performance`  
审计 HEAD：`cdad85004a53fd931d72bda168806362813c91bb`

## 结论

```text
INTEGRATOR_RECOMMENDATION=ACCEPT_MERGE_TO_FEATURE_BRANCH
PRODUCTION_USE=BLOCKED
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

TASK-004、TASK-005、TASK-006 的实现和审计证据满足当前 Core 集成门禁，允许
fast-forward 到 `codex/feature-session-engine-integration`。本次不执行生产
replay，不连接 Redis/TD/Rabbit，不重启服务，不修改生产目录或 effect 路径。

## 证据

- 工作树干净；任务分支从开发主线 `2de51f5` 线性推进。
- 变更范围仅为 Core 的 `src/`、`tests/`、`examples/` 与项目文档；未修改
  `engine-next`、`t1-v2`、`stock-situation-runtime`。
- 服务器 Python 3.12.3 验证：`660 passed`（3 个 protobuf 运行时弃用警告），
  `compileall` PASS，`git diff --check` PASS。
- TASK-004：500 frame ordered FRAME 约 458.8s，ordered FINAL 约 453.0s，
  both FRAME 约 454.4/456.4s；确定性比较和最终 FULL parity PASS。性能属于
  5–10 分钟 `PASS_WITH_WARN`，不是 ≤5 分钟的 clean PASS；frame 模式不宣称
  每帧 FULL parity。
- TASK-005：`ReplaySessionTimeline` 仅保存哈希和时间线元数据；保留 EMPTY
  frame，版本化竞价 revision，记录 timer/checkpoint；不拥有时钟、Provider、
  持久化、Rabbit、Redis/TD 或 effect。
- TASK-006：Rabbit parsed `DataRecord/DataBatch → RawTick/TickBatch` 为
  canonical authority；TD 仅为纯适配器。tick/batch hash scope、source/run
  evidence、proto3 default ambiguity、trade-date invariant、确定性 TD frame
  顺序与 order ambiguity 均有测试覆盖。
- canonical contract 包未导入 `pika`、Rabbit client、`redis`、`taos`、TD
  writer 或生产 service module；没有新增写入、ACK、重启或 effect。

## 保留限制

- `TD_WRITE_HEALTH=UNPROVEN`，因此 `M3_1_NORMAL=BLOCKED`；合并不改变生产门禁。
- Rabbit live payload、历史 arrival order、historical `available_at` 和生产
  batch equivalence 仍为 `UNVERIFIED`/`UNKNOWN`。
- TASK-004 的性能状态保留 `PASS_WITH_WARN`；后续如需 clean PASS，应单独做
  性能优化任务，不在本次合并中放宽语义或门禁。

## 下一任务

`TASK-007`（offline canonical replay / auction facts）登记为下一候选任务，
本次不自动启动、不创建新的生产接入或 recovery 写路径。
