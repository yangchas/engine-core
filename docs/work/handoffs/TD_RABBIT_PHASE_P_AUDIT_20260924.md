# TD/Rabbit global replay — Phase P audit (2026-09-24)

审计时间：2026-09-24 23:52（Asia/Shanghai）<br>
状态：`PHASE_P_PARTIAL`

## 目的

在不改生产 consumer/ACK、TD writer 或服务部署的前提下，验证“一个 3 秒
TD 读取片是否能作为 Rabbit 同形输入交给 exact t1-v2”。本轮允许向隔离
Redis 写入，TD 仍只读；使用当前 exact release，设置
`REPLAY_BATCH_SIZE=1000000`，以排除普通 batch-size 提示对结果的影响。

## 真实运行

- TD：`market_data1.stock_tick_v2`
- 交易日：`2026-09-24`
- 半开窗口：`[09:15:00, 09:40:00)`
- exact t1-v2 binary SHA-256：
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`
- Redis：DB `10`，唯一前缀 `task009p:`
- `REPLAY_WRITE_REDIS=true`、`REPLAY_WRITE_TDENGINE=false`
- 验证目录：
  `/home/exedev/validation/td-rabbit-phase-p-single-slice-20260924T232137629+0800/`

独立 TD `SELECT COUNT(*)` 为 `1,194,572`，与 t1-v2 `source_in/ticks` 完全
相等。t1-v2 汇总：

```text
batches=1224
source_in=1194572
source_reject=0
ticks=1194572
clocks=78
redis_cmds=2408279
td_sql=0
ack=0
quote_state_committed=1200751
max_batch_ticks=3648
```

## 语义结果

- DB10 Q2 hash 数 `5222`；Core 直接读取并进行 ordered/shuffled 重放。
- Core projection hash：
  `c1428e3771fdfd07e2fe7d68e2fdac11c86960a87cce3d87596fad34359b9a1b`。
- ordered/shuffled projection 与 4 个抽样 Engine hash 相等。
- 在观察时刻 `09:40:00`、显式 10 秒 freshness policy 下，`116` 个 quote
  stale；Core 结果为 `REPLAY_PARTIAL`，`normal_opening_pass=UNPROVEN`。
- 与 Phase O（DB9/`task009o:`）的 5222 个 Q2 语义 hash 完全相等，差异数
  `0`；0920、0924、0925 三个稳定 legacy auction projection 也逐项相等。
- DB0 的 `task009p:*` 数量为 `0`；TD 写入为 `0`；Rabbit consume/ACK 为
  `0`；服务仍为 `engine-next=active`、`t1-v2-live=active`。

## 关键合同发现：时间片读取通过，batch 形状仍未闭环

exact release 源码的事实是：

1. `query_next_slice()` 对每个 `[start,end)` 3 秒半开区间执行一次 SELECT，
   并将该片所有已返回行暂存在 `pending_records_`；这符合“不一次读取全天，
   每次只取一个时间片”的读取边界。
2. `emit_pending_batch()` 并不把整个片作为一个 `TickBatch` 发送，而是调用
   `replay_timestamp_group_size()`，按同一 `tss` 时间组逐组发送。源码行见
   exact release `td_replay_tick_source.cpp:198-225`。
3. 因此 `REPLAY_BATCH_SIZE=1000000` 只影响 `reserve()`，不能改变发送形状；
   本轮仍是 `1224` 个 batch，而不是 3 秒片数量对应的单片 batch。
4. `logical_ts_ms` 由每个时间组的最大 tick 时间派生，而不是整片结束边界；
   这是为防止 09:25 屏障把后续 tick 带入快照的现有保护，但它尚未证明与
   Rabbit 的真实 delivery 分批相同。

### 单片实测

为排除“全窗口统计掩盖片内分组”的可能性，使用同一 exact release、同一
真实 TD、`REPLAY_BATCH_SIZE=1000000` 和加速后的虚拟等待，单独运行
`[09:25:00,09:25:03)`：

```text
TD rows                         4681
t1-v2 batches                  3
max_batch_ticks                3174
redis_cmds                     9384
td_sql                         0
ack                            0
isolated Redis                 DB11/task009p3:
```

同一窗口的只读 TD 分组为：

```text
09:25:00  3174 rows
09:25:01   739 rows
09:25:02   768 rows
```

因此这不是偶然的全窗口计数：一个 3 秒读取片被 exact release 明确拆成了
三个同 `tss` 处理 batch。该行为可以保留事件时间屏障，但不能直接称为
“一个 3 秒全市场 batch 与 Rabbit DataBatch 同形”。单片证据目录为
`/home/exedev/validation/td-rabbit-phase-p-single-0925-20260924T235445283+0800/`。

所以本阶段结论是：

```text
TD_3S_SELECT_BOUNDARY              PASS
ALL_SOURCE_ROWS_PRESERVED          PASS
T1_V2_Q2_REAL_REPLAY               PASS
ISOLATED_REDIS_READBACK            PASS
Q2_AND_AUCTION_REPEAT_DETERMINISM  PASS
RABBIT_SHAPED_BATCH_EQUIVALENCE    UNPROVEN
NO_COMPUTE_CHUNK_CONTRACT          NOT_CLOSED
NORMAL_OPENING                     UNPROVEN
```

这不是数据或 Q2 计算失败；是“回放时间片输入”和“Rabbit delivery batch 形状”
仍未收口。不能把按 `tss` 分组的 1224 个 batch 说成真实 Rabbit 的 3–4 个
delivery，也不能从 TD 事件时间推断 arrival order、completion watermark 或
historical `available_at`。

## 下一道门禁

在不碰 live consumer/ACK 的前提下，下一项只能是 replay-only batch-contract
设计与测试：

- 明确一个 3 秒片是否以单个 `TickBatch` 进入 t1-v2，或由一个不改变顺序的
  片内处理器承担 09:25:06 的事件时间屏障；不能继续把 `tss` 分组当作 Rabbit
  delivery 证据。
- 用真实片内 `05.xxx/06.xxx/07.xxx`（若数据源存在）验证 09:25 锚点不包含
  07 秒之后的 tick；没有真实混合样本时保持 `UNVERIFIED`。
- 同一真实输入比较“单片”和“模拟多 delivery”的 Q2、auction state、冻结
  锚点和 Redis 语义 hash；差异必须解释为批次/时钟语义，不能静默吞掉。
- 该设计经主线对齐审计通过后，才允许重编 replay-only binary；不自动部署，
  不修改 Rabbit schema/consumer/ACK。

## 证据文件

- `phase_p`：`t1_v2_stdout.txt`、`t1_v2_stderr.txt`、`td_count.json`
- `q2_capture_094000.json`、`core_opening_094000.json`
- `redis_repeat_comparison.json`
- `db0_isolation.json`、`runtime_status.txt`
- `sha256sums_all.txt`

本审计使用 ECC `contract-first` 与 `production-audit` 约束；没有把 Redis
隔离写入、TD 事件顺序或 Core 确定性错误升级为生产 Rabbit 等价或 M3-1 通过。
