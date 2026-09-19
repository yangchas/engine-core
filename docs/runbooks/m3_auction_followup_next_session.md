# M3 09:24 / 09:25 Core follow-up shadow 运行单

本运行单只用于下一个交易日的真实只读验证。生产 owner 仍为
`engine-next`/`t1-v2-live`，不得新增 Rabbit consumer、改变 ACK、写 Redis/TD
或触发邮件/通知/effect。

## 盘前停止条件

在 Cobra-ion 上确认：

```bash
date -Is
systemctl is-active engine-next t1-v2-live
df -h / /home/exedev
```

如果服务不 active、t1-v2 没有最近 heartbeat、磁盘仍处于容量阻塞，或目标
交易日不在日历快照中，记录 `BLOCKED`，不要用 `RECOVERY_CATCHUP` 冒充正常验证。

## 09:24 NORMAL

`09:24:00` 是最早检查时间，不是迟到数据的失效时间。正常盘中优先在
`09:24:00` 后运行；若数据读取或进程启动延迟，允许在后续时间以实际读取时刻
执行，并在结果的 `timing.late_execution=true` 中保留迟到事实。`observed_at` 和
`as_of` 使用同一次远端时钟读取：

```bash
cd /home/exedev/validation/engine-core-m3-followup-20260918
PY=/home/exedev/services/engine-next/shared/venv/bin/python
OBSERVED_AT="$(date -Is)"
"$PY" examples/run_m3_auction_followup_shadow.py \
  --trade-date YYYY-MM-DD \
  --symbol 000338 \
  --node-tag 0924 \
  --calendar-file /home/exedev/validation/cc-m0-calendar-20260911-v3.json \
  --observed-at "$OBSERVED_AT" \
  --as-of "$OBSERVED_AT" \
  --origin NORMAL \
  --output /home/exedev/validation/m3-0924-shadow-YYYYMMDD-000338.json
```

验收：`preflight_gate=PASS`、`node_dispatched=true`、projection tag/交易日正确、
source time 不晚于 cutoff、`side_effect_boundary` 仍为 Redis read + in-memory
Core。`startup_self_check.status` 可能保留 Q2 缺失，但必须同时有
`node_readiness=DISPATCHABLE` 和
`q2_policy=OPTIONAL_FOR_SOURCE_OWNED_AUCTION_NODE`；这不是把 Q2 缺失升级为 READY。

## 09:25 NORMAL

`09:25:00` 是业务锚点，但生产行情在该时刻尚未完成全市场补齐。`09:25:06`
只是最早检查屏障，不是 source timestamp 或读取时间的截止线；数据晚到时允许
在 `09:25:06` 之后读取并执行。`source_record_time` 保留源的真实时间，
不得改写成 `09:25:00`。当前调用必须同时读到并校验
`0920`、`0924`、`0925` 三个标签；不得用缺失槽位、后续 repair 或其它日期代替：

```bash
OBSERVED_AT="$(date -Is)"
"$PY" examples/run_m3_auction_followup_shadow.py \
  --trade-date YYYY-MM-DD \
  --symbol 000338 \
  --node-tag 0925 \
  --calendar-file /home/exedev/validation/cc-m0-calendar-20260911-v3.json \
  --observed-at "$OBSERVED_AT" \
  --as-of "$OBSERVED_AT" \
  --origin NORMAL \
  --output /home/exedev/validation/m3-0925-shadow-YYYYMMDD-000338.json
```

## 结果分类

分别记录：

```text
M3_0924_NORMAL = PASS | BLOCKED | WARN
M3_0925_NORMAL = PASS | BLOCKED | WARN
```

`NORMAL` 结果可以在业务窗口结束后迟到完成，但必须保留实际
`observed_at/as_of/source_record_time`，不能伪装成准点结果。盘后或晚启动重跑
统一标记 `RECOVERY_CATCHUP`；恢复读取以本次实际 evaluation time 做 source
future 校验，不把 `09:25:06` 当作历史 source cutoff。若 `as_of` 尚未到该
barrier，仍然 `BLOCKED`。只有明确要求重建“当时 cutoff 可见数据”的历史回放，
才继续使用严格 knowledge cutoff 和 availability 证据。本运行单只验证节点接入，
不宣称 source-freeze ownership 或完整 Auction Shadow 规则迁移。
