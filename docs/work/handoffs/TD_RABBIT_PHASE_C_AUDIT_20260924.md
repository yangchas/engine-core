# TD/Rabbit global replay Phase C audit

审计时间：2026-09-24<br>
当前分支：`codex/feature-session-engine-integration`<br>
当前 Core HEAD：`46515c0c70ba6bee06deef1f5ebfb260e0a57eee`

## 阶段结论

```text
PHASE_B_CANONICAL_SLICED_READ=PASS
PHASE_C_T1V2_Q2_BRIDGE=PASS_FOR_REAL_FULL_WINDOW
PHASE_C_OVERALL=PARTIAL
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
REDIS_ISOLATED_PROJECTION=NOT_STARTED
09_25_06_LIVE_BARRIER=UNPROVEN
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## 已证明的事实

1. Core 真实 TD reader 对 09:15–09:40 执行了 500 个 `[start,end)` 3 秒
   半开查询，只保留当前片；真实返回 1,224,811 行，98 个空片，5,221
   个 symbol，VirtualClock 到 09:40。canonical ordering 修复后局部测试和
   全量真实 ordered 复核均通过。
2. exact 当前 t1-v2 release 在同一真实 TD 窗口以 `--q2frame` 本地模式运行，
   `source_in=1,224,811`、`source_reject=0`，形成 1,205 个 source-time
   batches、1,226,603 个 Q2 updates 和 98 个 clock events。Redis/TDengine
   写入、Rabbit consume/ACK 均跳过。
3. Core 对冻结 Q2Frame 文件重复运行两次，文件 SHA-256 在 inventory、两次
   回放之间和结束时一致；frame/projection/final-state hash、signals、reducer
   revision、coverage 和 VirtualClock 全部一致，结果为
   `REPLAY_READY_BOUNDED`。

## 主线对齐

```text
Rabbit-primary input shape        已由 exact t1-v2 输出字段继续作为目标
TD read one 3s slice              PASS（Core TD供给）
same t1-v2 Q2 producer            PASS（真实 exact release，本地 q2frame）
replay no TD persistence          PASS
Redis production write            未执行
Rabbit consumer/ACK               未执行
09:25:06 wall-clock freeze        尚未验证
```

特别保留一个边界：3 秒 TD read slice 不是 t1-v2 processing batch。t1-v2 按
source `tss` 分批，不能把 1,205 个真实 source batch 改写成 500 个 synthetic
delivery，也不能将 deterministic replay order 解释为 Rabbit arrival order。

## 未证明项

- 当前环境没有真实 Rabbit delivery capture，因此 batch membership、arrival
  sequence、watermark 和 ACK 时序保持 `UNKNOWN`；
- `--q2frame` 只验证 t1-v2 计算结果的隔离文件，不代表生产 Redis key/TTL 或
  Redis 提交成功；
- 09:25:06 对跨秒/跨 source batch 的冻结，需要独立的事件时间重建与实盘
  wall-clock 对照；
- TD 曾出现空间告警。当前磁盘约 22G 可用、服务 active，但不能以此证明
  `TD_WRITE_HEALTH=PROVEN`；
- historical `available_at` 没有从 logical/source time 推断。

## 下一阶段闸门

在用户确认继续前，保持 TASK-008 `PARTIAL_EVIDENCE` 和本计划
`PHASE_C_PARTIAL`。下一项应是 09:25:06 屏障/冻结候选的真实数据验证，并且
只在隔离输出中比较内存 Q2、生成命令与锚点；不得写生产 Redis、TD、Rabbit 或
重启服务。若没有 arrival/available_at 证据，结论必须继续是 `PARTIAL/UNKNOWN`，
不能升级为 NORMAL 或生产等价。

## 证据

- `/home/exedev/validation/td-rabbit-phase-b-full-canonical-20260924T192010+0800/phase_b_canonical_report.md`
- `/home/exedev/validation/td-rabbit-phase-c-q2-full-20260924T014403+0800/phase_c_full_report.md`
- `/home/exedev/validation/td-rabbit-phase-c-q2-2slice-20260924T014004+0800/phase_c_report.md`
- `docs/work/plans/TD_RABBIT_GLOBAL_3S_REPLAY.md`
