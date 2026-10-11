# TASK-008 opening stale-Q2 cohort: cross-date observation

审阅日期：2026-10-10（Asia/Shanghai）

性质：对已保存的真实 t1-v2 Q2Frame 输出和 Core 报告做离线、观察性比较；不是 live 09:32 验收，也不改变任务状态。

## 结论

三份报告都在 `09:32:10 Asia/Shanghai` 评估，使用 `stale_after_ms=60000`。按报告 `OPENING_0932.facts_by_symbol` 中每个标的的 `timestamp_ms` 重新计算，stale 数分别为 `12/12/7`，与报告计数一致。三日共有 5 个重复 stale 标的，三日并集为 16 个。

这里的 `stale` 仅表示报告中的 Q2 fact 时间戳在评估时超过 60 秒；不表示没有行情、不表示没有 tick/Rabbit 消息，也不解释其原因。此观察不能证明实盘 09:32 cohort、全市场覆盖、Rabbit 到达顺序、NORMAL 等价性或停牌状态。

## 固定输入与报告

下列 report 和 Q2Frame SHA-256 均在本次审阅中对实际文件重新计算。报告中的 `q2frame.sha256` 与对应 Q2Frame 文件实际 SHA 一致。

| 实际交易日 | 报告（绝对路径） | Report SHA-256 | Q2Frame 输入（绝对路径） | Input SHA-256 | 契约 / universe | `OPENING_0932` 输入 |
|---|---|---|---|---|---|---|
| 2026-09-23 | `/home/exedev/validation/task008-current-head-full-replay-20261007-CJLfBg/core-through-input.json` | `e81ed19b45f7ffe43c4899d8dacc8ae7486520901d83061cb9e42102b9a08206` | `/home/exedev/validation/task008-current-head-full-replay-20261007-CJLfBg/slice-q2frames.jsonl` | `c6317dcf444ca4b54c36cba43713294ccdf04ba325c1e2abb603c29b67d5a1a4` | `Task008Q2FrameSessionEngineShadowV14` / 5,222 | 343 frames, 436,327 updates; last raw time 09:32:08 |
| 2026-09-29 | `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/core_auction_opening_shadow.json` | `6fdc8485e9877b9f136148b594b1c77cac963868ab5fc27818d68bb2c11f180c` | `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl` | `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9` | `Task008Q2FrameSessionEngineShadowV3` / 5,223 | 734 frames, 418,759 updates; last raw time 09:32:10 |
| 2026-09-30 | `/home/exedev/validation/task008-0930-current-core-replay-20261008T015810+0800/core_q2frame_report.json` | `d0f7adbe5ba46b7aca3b26535ff2ec39461f97312f4547b42f49d58974c2cfe7` | `/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/deployed_release_q2frame_to_0932.jsonl` | `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a` | `Task008Q2FrameSessionEngineShadowV12` / 5,220 | 754 frames, 428,586 updates; last raw time 09:32:10 |

三份输入的完整 JSONL 行数分别为 `500/735/758`；首末 `logical_ts_ms` 对应本地时间分别为 `09:15:02–09:39:59`、`09:15:00–09:32:11`、`09:15:00–09:32:14`。这些是完整输入文件范围；上表的 frame/update 数是各报告实际纳入 `OPENING_0932` 计算的数量。输入 JSONL 没有顶层 `trade_date`；交易日依据报告正文的 `trade_date`，并以首末逻辑时间日期交叉核对，不能依据目录名或文件名推断。

三个报告契约版本不同（V14/V3/V12），因此下面是同一 `Task008Q2FrameSessionEngineShadow` 家族内的观察性集合比较，不是同版本回归或严格同代码比较。universe 数也略有不同。

本次只纳入本轮清点中同时满足以下条件的三份存档：报告正文的实际 `trade_date` 可确认、逐标的 `facts_by_symbol` 可用、Q2Frame 输入 SHA 已核对。其他候选存档未纳入；例如实际交易日未从报告正文确认，或没有逐标的事实列表的材料。本次不替代这些候选日期，也不声称已覆盖所有可用交易日。

## 判定口径

对每个报告：

```text
age_ms = evaluation_time_ms - facts_by_symbol[symbol].timestamp_ms
STALE = age_ms > stale_after_ms
```

三份报告的 `evaluation_time_ms` 分别对应 `2026-09-23/29/30 09:32:10 +08:00`，`stale_after_ms=60000`。下面矩阵只覆盖三日 stale 集合的并集。`≤60s` 表示该报告有非空时间戳且按年龄计算未 stale；它不代表该 fact 的其他字段完整或 `READY`。`无 fact 项` 表示该 symbol 未出现在该日期报告的 `facts_by_symbol` 中，不能当作 fresh，也不推断其原因。

这里比较的 `facts_by_symbol.timestamp_ms` 是报告所记录的 Q2 fact 来源时间，不是 Q2Frame 行的 `logical_ts_ms`。例如 09-23 输入首帧的逻辑时间为 09:15:02，而矩阵中的部分来源时间为 09:15:00；两个时间字段分别保留、不能互相替代。

## 16 个标的的逐日矩阵

单元格中的时间是报告记录的 Q2 fact `timestamp_ms` 转为本地时间；括号中的 `mm:ss` 是距该日 `09:32:10` evaluation time 的分钟和秒数（例如 `17:10` 表示 17 分 10 秒）。`≤60s` 单元格为节省表宽不列具体来源时间和年龄。

| Symbol | 2026-09-23 | 2026-09-29 | 2026-09-30 |
|---|---|---|---|
| `000016` | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） |
| `002731` | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） | 无 fact 项 |
| `002860` | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） | 无 fact 项 |
| `300082` | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） |
| `300527` | ≤60s | STALE 09:15:00（17:10） | ≤60s |
| `300585` | STALE 09:15:00（17:10） | ≤60s | ≤60s |
| `301139` | STALE 09:15:00（17:10） | STALE 09:15:00（17:10） | ≤60s |
| `600293` | ≤60s | STALE 09:15:21（16:49） | STALE 09:15:28（16:42） |
| `600363` | ≤60s | STALE 09:15:12（16:58） | ≤60s |
| `601059` | STALE 09:15:16（16:54） | STALE 09:15:12（16:58） | STALE 09:15:25（16:45） |
| `601198` | STALE 09:15:11（16:59） | STALE 09:15:04（17:06） | STALE 09:15:11（16:59） |
| `601238` | STALE 09:15:17（16:53） | ≤60s | ≤60s |
| `603183` | ≤60s | ≤60s | STALE 09:15:13（16:57） |
| `603400` | STALE 09:15:04（17:06） | STALE 09:15:04（17:06） | ≤60s |
| `605303` | STALE 09:15:04（17:06） | ≤60s | ≤60s |
| `688496` | STALE 09:15:04（17:06） | STALE 09:15:01（17:09） | STALE 09:15:11（16:59） |

每条 stale 观察的原始 `limit_state` 都是数值 `0`，且对应 `limit_state_status=available`。本报告保留该原始值，不将其解释为正常、平盘、停牌或其他市场状态。

## 集合比较

- stale 数：2026-09-23 `12`，2026-09-29 `12`，2026-09-30 `7`。
- 三日交集（5）：`000016, 300082, 601059, 601198, 688496`。
- 三日并集（16）：矩阵中的全部标的。
- 两两交集大小：09-23∩09-29=`9`，09-23∩09-30=`5`，09-29∩09-30=`6`。
- 仅在该单一日期 stale 的标的：09-23=`300585, 601238, 605303`；09-29=`300527, 600363`；09-30=`603183`。
- 09-30 的 `facts_by_symbol` 没有 `002731`、`002860` 两项；矩阵标为“无 fact 项”，而不是将其算作 fresh。原因未知。

## 限制与对齐

- “stale”来自 Q2 fact 时间戳年龄，不代表期间没有原始 tick 或 Rabbit 消息。生产者在字段无变化时可能不发新的 Q2 更新；这与时间戳长期不变相容，但本次材料不能证明这些标的具体发生了什么。
- 先前对 reviewed release source 的代码审阅记录在 [TASK-008 REAL 09:32 Q2Frame handoff](../work/handoffs/TASK-008-REAL-0932-Q2FRAME-20260930.md) 中，指出 `DIRTY_NONE` 时 writer 会跳过 Q2 更新（该审阅记录的源文件 SHA-256 为 `9baa218adb6f00cf7ae85929659c74ff9d01ce4882bc9180cc1ef46d9e586940`）。它是一个可能解释，不是本次三个交易日逐 tick dirty-mask 的直接证据；精确原因仍为 `UNKNOWN`。
- 三份 Q2Frame 的输入产出构建身份均为 `UNKNOWN`，可能不同；文件名或报告日期不能证明使用了同一 producer/Core 构建。
- 三个 contract 版本及 universe 不完全相同，集合差异不能归因于市场行为或 Core/producer 回归。
- 这是对已落盘真实来源产物的离线读取，本次没有重新连接 Redis、TDengine 或 RabbitMQ；不宣称此次完成 live source 验证。
- `limit_state=0` 的业务含义没有在此处验证；缺少停牌/证券状态独立参考，不能推断停牌、无行情或成因。
- 不证明 Rabbit arrival order、完整全市场覆盖、历史 `available_at`、实时 09:32 cohort、NORMAL 行为或生产等价性。

项目状态不变：

```text
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

本报告不增加门禁、不升级验收、不更改代码或任务板状态。
