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

必须在 `09:24:00 <= as_of < 09:25:00` 执行，`observed_at` 和 `as_of` 使用同一
次远端时钟读取：

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

`09:25:00` 是业务锚点，但生产行情在该时刻尚未完成全市场补齐。正常
`0925` finalization 只有在 settling barrier 之后才允许执行，因此必须在
`09:25:06 <= as_of < 09:26:00` 执行。`source_record_time` 保留源的真实时间，
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

只有真实窗口内的 `NORMAL` 结果才可作为正常节点证据。盘后重跑统一标记
`RECOVERY_CATCHUP`，若 projection 的 observed/source time 晚于业务 cutoff，必须
`BLOCKED`；其中 `0925` 的 recovery source cutoff 是 `09:25:06`，不是 Timer 的
`09:25:00` scheduled time。若晚启动时 `as_of` 尚未到该 barrier，也必须
`BLOCKED`。本运行单只验证节点接入，不宣称 source-freeze ownership 或完整
Auction Shadow 规则迁移。
