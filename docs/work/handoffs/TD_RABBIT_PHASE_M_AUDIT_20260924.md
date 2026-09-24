# TD/Rabbit global replay — Phase M audit

审计时间：2026-09-24 21:42:28 +08:00<br>
目标日期：2026-09-24<br>
窗口：`09:15:00 <= ts < 09:25:09`<br>
状态：`PHASE_M_PARTIAL`

## 结论

本阶段首次完成了同一交易日的真实 TD → 当前 t1-v2 发布包 → 隔离 Redis →
Core 只读回读。它证明了回放数据可以写入隔离 Redis，并由 Core 读取；没有
写 TD、消费 Rabbit 或修改生产 DB0。它不证明 Rabbit delivery/arrival 等价，
也不把正常开盘 acceptance 提升为 PASS。

```text
TD_T1V2_ISOLATED_REPLAY=PASS
CORE_REAL_Q2_READBACK=PASS_WITH_PARTIAL_FRESHNESS
REDIS_PRODUCTION_DB0_SIDE_EFFECT=NONE_OBSERVED
TD_WRITE=0
RABBIT_ACK=0
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

## 真实运行

- TD 独立 `SELECT COUNT(*)`：`210,730` 行；t1-v2 `source_in=ticks=210,730`。
- 当前发布包：`/home/exedev/services/t1-v2/current/content/bin/t1_v2`，SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`。
- t1-v2：`604` 个 source-time batch、`1` 个 09:25:06 Clock、
  `425,676` 个 Redis command、`redis_committed=210,718`、`td_sql=0`、`ack=0`、
  `source_reject=0`、`max_batch_ticks=3478`。
- 运行目录：
  `/home/exedev/validation/td-rabbit-phase-m-0924-0915-0925-20260924T210300+0800/retry/`
- Redis 写入目标：DB1，唯一前缀 `task009m:`；完成后 `5,233` 个前缀 key，
  其中 `5,222` 个 Q2 hash。
- 生产 Redis DB0：回放前缀命中数 `0`，未执行写入。
- `REPLAY_WRITE_TDENGINE=false`；本阶段没有 TD 写入。

## Core 回读

把 DB1 的 Q2 hash 转为 Core 既有 canonical capture 后，按
`2026-09-24T09:25:09+08:00`、`stale_after_ms=10000` 做 ordered/shuffled
两次回读：

- `5,222/5,222` observed，coverage `1.0`，missing `0`；
- `549` 个 quote 超出 10 秒 freshness，故状态为 `PARTIAL`，不是数据缺失；
- projection content hash：
  `fa978984f60ec345edcf8134c0a6a3834df2e9ce3cbf7ec9d7d07acf18f4ac5c`，两次相等；
- 采样 `000001/000002/000338/600519` Engine hash 全部相等；
- `production_side_effects=NONE_OBSERVED`；`normal_opening_pass=UNPROVEN`。

对应文件：

- `core_opening_readback_barrier_plus3s.json`
- `q2_capture.json`
- `t1_v2_stdout.txt` / `t1_v2_stderr.txt` / `exit_code.txt`
- `sha256sums.txt`（`sha256sum -c` 全部 `OK`）

同一窗口随后在 Redis DB6/`task009m2:` 重复运行：计数仍为
`210730/604/1/425676/td_sql=0/ack=0`，两次各有 `5233` 个隔离 key。除
`m2:runtime:20260924` 中 Redis runtime counter/byte 指标外，规范化 key 集合
完全相同；两次 normalized semantic SHA-256 均为
`17d8f3f176116d8578559713fcfcf0d9d5e95294f74e00b3c4e85356acd3c28f`。
比较证据：`redis_repeat_comparison.json`。

## 与生产 Redis 的解释性对照

生产 DB0 是当前时刻的滚动 Q2，已经晚于 09:25:09，不能拿来要求逐字段相等。
只作集合和冻结投影的诊断：

- DB0 Q2 hash：`5,225`；DB1 回放 Q2：`5,222`；共同集合 `5,222`。
- DB0 多出的 3 个 key 是已知的当天占位/陈旧对象，不属于该 TD 窗口的有效源集合。
- 共同 Q2 的 `ts/amt/vol/phase` 等滚动字段大范围不同，这是截止时间不同的预期，
  不能归类为回放失败。
- `a2:20260924:0925`：两边 `n=5208`；summary 除写入毫秒外完全一致，
  `top_amt` 集合和顺序完全一致；`top_chg` 只有边界并列的 2 条记录不同，
  属于未冻结 tie-break/到达顺序差异，不宣称 strict parity。
- `0920/0924` 的 `n` 与 summary 存在真实可见集合差异（0920 回放 `4869`、
  DB0 `4865`；0924 两边 `5116`），保留为 PARTIAL，不补造数据。

TD `auction_snapshot_v2` 同日 09:25 源集合为 `5,222` 行；隔离回放生成的 0925
锚点为 `5,208` 行。抽查的 match/rest 字段与 TD 同口径行一致，NULL 价格仍保留
为不可用事实；锚点不包含 09:25:07 之后的 tick。

随后完成同日全量逐股核对：DB1 anchor 与 TD 共有 `5,208` 行，match amount
和 rest bid 均为 `5,208/5,208` 一致；缺少的 14 行全部是 TD 中
`px/chg=NULL` 且 match/rest 全为 0 的不可用事实，不是 Q2 丢失。anchor
本身不承载 rest ask/price/limit 的完整可比字段，因此这些字段保持
`NOT_COMPARABLE`，没有被零填充或推断。anchor 内容 SHA-256 为
`52b2c8f0faf58e2db3efaac69a1c86516be9b6ff6c4ed60016dda04179169884a`。

## 安全与服务状态

- `engine-next=active`，`t1-v2-live=active`；本阶段未重启任何服务。
- 根分区约 `44%` 使用，约 `22G` 可用。
- 本阶段使用的 Redis 写入是用户明确批准的隔离 DB/前缀；生产 DB0、TD、Rabbit
  均未被写入/消费。

## 适配问题记录

第一次 Core 回读直接把 Redis 原始短字段喂给 canonical capture loader，得到
全量 `MISSING`。这被识别为测试 capture 字段映射错误，随后按 Core canonical
字段重建 capture 并成功得到 `5,222` 行；不把第一次适配错误计入生产数据结论。

## 未关闭项

1. 没有 Rabbit 原始 capture，无法证明 delivery membership、arrival order 或
   historical `available_at`。
2. `top_chg` 并列边界的 tie-break 未冻结；不能把 0925 诊断提升为 strict
   full projection parity。
3. Redis 当前滚动 Q2 不能作为 09:25 历史权威；冻结锚点才是本次回放证据。
4. `M3_1_NORMAL` 与 `TD_WRITE_HEALTH` 仍由独立生产门禁决定。

## 09:25:06 屏障事实补充

对同一日 TD 做了只读分段核对：

- `[09:25:00,09:25:03)`：`4,681` 行、`4,673` symbols；
- `[09:25:03,09:25:06)`：`1` 行、`1` symbol；
- `[09:25:06,09:25:09)`：`0` 行、`0` symbols。

DB1 Q2 的最大 source timestamp 是 `09:25:03`，而冻结 anchor 的 meta timestamp
是 `09:25:06`。因此本次真实日期证明了“先处理 barrier 前最后一条 tick，再由空片
Clock 在 09:25:06 冻结”的路径；`09:25:09` 运行没有可污染 anchor 的 06–09 秒
市场 tick。真实 `05.xxx/06.xxx/07.xxx` 混合 delivery 在现有历史数据中不存在，
所以该场景仍为 `UNVERIFIED`，不能用合成 fixture 冒充真实 Rabbit 证据。

当前 exact t1-v2 replay scheduler 使用半开 3 秒片并要求终点对齐：若
`end=09:25:06`，只会处理到 `[09:25:03,09:25:06)`，不会再查询空的
`[09:25:06,09:25:09)`，因而不会产生 09:25:06 Clock。要在无 06 秒行情时
复现真实的 09:25:06 冻结，回放终点必须至少延伸到 `09:25:09`；这不是读取
额外市场数据，而是让控制面空片推进时钟。

跨日只读检索（2026-09-18 至 2026-09-24）显示每个日期的
`[09:25:06,09:25:09)` 均为 0 行。因此当前 TD 历史没有真实的
`05.xxx/06.xxx/07.xxx` 混合样本；该项只能等待真实 Rabbit capture 或未来有
对应事件时间的 TD 数据，不能用合成 fixture 提升为生产 arrival 结论。
