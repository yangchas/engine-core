# TD/Rabbit global replay Phase E audit

审计时间：2026-09-24 19:42 +08:00<br>
当前分支：`codex/feature-session-engine-integration`<br>
当前 Core HEAD：`46515c0c70ba6bee06deef1f5ebfb260e0a57eee`（另有未提交的合同修复）

## 阶段结论

```text
REAL_TD_SOURCE_READ=PASS
T1V2_REAL_Q2_PROCESSING=PASS
REDIS_ISOLATED_PROJECTION=PASS
09_25_06_EMPTY_SLICE_BARRIER=PASS
REDIS_PROJECTION_DETERMINISM=PASS
PHASE_E_OVERALL=PARTIAL
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## 真实证据

- 2026-09-18 `09:25:00–09:25:09` 真实 TD 回放：6041 条 tick、5 个
  t1-v2 source-time batch、1 个真实空片 Clock；`redis_cmds=12126`、
  `td_sql=0`、`ack=0`。
- 只写 Redis DB14，隔离前缀 `task009e2:`，产生 5172 个 Q2 key、0925
  A2/latest、runtime 和独立 anchor；生产 DB0 无 `task009e*` key，DB0
  规模仍为 12269。
- 同输入 DB13/`task009e3:` 重复回放，去除隔离前缀后的 Redis 内容哈希相同：
  `4fb99dc2dea434f8496ed35dc8405e9b5ee1e1c85b206442261c25fddfe34566`。
- A2 0925 元数据时间为 `09:25:06`；`[09:25:06,09:25:09)` 无 TD tick，
  说明快照由 Clock 屏障触发，不依赖下一条行情。
- `auction_snapshot_v2` 的 0925 真实源时间为 `09:25:06.197`，5221 行；
  09:25 窗口五档候选 5171/5221，非 NULL 价格可比项 4408/4408。

完整输出：

```text
/home/exedev/validation/td-rabbit-phase-e-redis-20260924T193920+0800-barrier/
```

## 合同修复

真实 t1-v2 `snapshot_trigger.cpp` 在 `hms >= 92506` 发出 0925。Core 原先
将 `freeze_time_ms` 推迟到 `preferred_finalize=09:25:10`，本阶段已改为：

```text
freeze_time_ms = first_observable_ms (09:25:06)
preferred_finalize_ms = 自适应宽限参考，不是首个快照硬门槛
```

迟到 cohort 仍由 revision/content hash 机制保留，不覆盖旧证据。Core 全量
测试、compileall、diff-check 均通过。

## 主线对齐与未证明项

```text
Rabbit-primary input shape        保持
TD one 3s read slice              保持；t1-v2 内部按 source tss 分批
same t1-v2 Q2 producer            真实发布包验证
replay Redis                      仅隔离 DB/前缀写入
TD write                          关闭
Rabbit consume/ACK                未发生
09:25:06 freeze                   回放 Clock 已证明；live 可见集合仍 UNKNOWN
```

本阶段不升级 TASK-008 为 NORMAL，也不改变 M3-1：

```text
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

下一步是审计 09:25 锚点字段映射、跨屏障同输入分批差异和 live/replay 可见性，
不得消费 Rabbit、写 TD 或部署生产。
