# TD 3 秒全市场回放 → Rabbit 同形输入 → t1-v2 Q2/竞价验证计划

Phase P update: the current state is `PHASE_P_PARTIAL`. A same-day real
2026-09-24 `09:15:00–09:40:00` TD window was replayed through the exact
t1-v2 release into isolated Redis DB10/`task009p:` with
`REPLAY_BATCH_SIZE=1000000`. The independent TD count and t1-v2
`source_in/ticks` were both `1,194,572`; the run still produced `1,224` batches,
because the exact release emits one `tss` timestamp group at a time after each
3-second query. Q2 semantic content and stable 0920/0924/0925 auction outputs
matched Phase O exactly, and Core ordered/shuffled replay was deterministic.
This proves real Q2 repeatability, not Rabbit delivery-shape equivalence; the
no-compute-chunk contract remains open. Evidence:
`docs/work/handoffs/TD_RABBIT_PHASE_P_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-p-single-slice-20260924T232137629+0800/`.

A bounded real single-slice check of `[09:25:00,09:25:03)` then confirmed the
exact release emits three `tss` groups (`3174/739/768` rows) as three t1-v2
batches, despite `REPLAY_BATCH_SIZE=1000000`. This is the concrete open
batch-shape finding; see
`/home/exedev/validation/td-rabbit-phase-p-single-0925-20260924T235445283+0800/`.

Phase P barrier experiment (2026-09-25) then used a validation-only binary on
the real same-day window `[09:15:00,09:25:09)`, with one TD SELECT per 3-second
slice and processing boundaries only at `09:20:03`, `09:24:10`, `09:25:06`,
and `09:32:10`. It wrote isolated Redis DB13/`task009pbarrier:` and processed
210,730 real ticks in 204 batches plus one empty-slice Clock, with `td_sql=0`
and `ack=0`. Q2 normalized SHA, 0920/0924/0925 legacy projections, latest,
and the 0925 anchor all matched the exact-release Phase M baseline byte/semantic
content. The earlier uncorrected one-batch-per-slice validation had 09:20
`n=4875` vs baseline `4869` and 09:24 `n=5117` vs `5116`; the barrier-aware
run restored both. Core DB13 readback was deterministic (`5222/5222`, coverage
`1.0`, 549 stale under the explicit 10-second policy, `FACT_ONLY`). This is a
real validation of the in-slice business-barrier hypothesis, not proof of
Rabbit delivery membership, arrival order, or historical `available_at`.
Evidence: `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_EXPERIMENT_20260925.md`

第二个真实交易日复测（2026-09-23）已完成：同一 validation binary 以 DB14/
`task009pbarrier23:` 重放 `[09:15:00,09:25:09)`，203 个 3 秒片、212022
行、1 个空片，片内 24 个片存在同股多事件时间。204 个 t1-v2 batch 加一枚
Clock，`td_sql=0`、`ack=0`；Q2、0920/0924/0925/latest/anchor 和 A2
均与 DB5/`task009k:` 精确基准相等，Core 在同一观察时刻读回 coverage 1.0、
missing 0、stale 154，重复 hash 相等。该结果把 barrier-aware whole-slice
假设从一个真实日期复核到两个真实日期，但仍保持 `PHASE_P_PARTIAL`：Rabbit
delivery membership/arrival order、completion watermark、historical
`available_at` 和 NORMAL opening 未证明。证据：
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER23_EXPERIMENT_20260925.md`
and `/home/exedev/validation/td-rabbit-phase-p-barrier-run-20260925T010000+0800/`.

**最新复核（2026-09-25，t1-v2 当前开发源码提交 `e91a20a`）**：在真实
2026-09-23 `[09:15:00,09:25:09)` 上运行三次，真实源输入均为 212022；两次
退出码明确为 0，第三次退出码未捕获但命名空间完整。三组隔离 Redis DB15
前缀去除全局 `m2:runtime.redis_bytes` 后的语义 SHA 完全一致，DB0 前缀命中为
0；Core 读回均为 5222/5222、coverage 1.0、missing 0、stale 154、`PARTIAL`。
但与旧 DB5/`task009k:` 的逐项比较发现 5206 个 Q2 hash 值不同；0920/0924
锚点计数各少 1，0925 数量相同，`latest` 从旧基线 09:25:03 推进到本轮空片
时钟 09:25:09。根因尚未证实，不能沿用之前 validation-only binary 的旧基线
parity 结论。当前状态仍为 `PHASE_P_PARTIAL`，见
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_CORRECTED_AUDIT_20260925.md`。

**Source-alignment correction (2026-09-25):** a read-only audit subsequently
found that development commit `e91a20a` does not use the same calculation
source as deployed release `20260923_tdstop0945b`. Auction matching/rest
amounts, limit-state reference price, and Engine clock/session handling differ;
the release's declared base commit is unavailable in the local t1-v2 Git
object database, and that repo has no remote configured. These are plausible
confounders for the observed Q2/A2 deltas, not a proven full cause. A
validation-only hybrid replay has since used release calculation files with the
current 3-second reader/barrier on the same real 2026-09-23 window. It matched
all 5222 Q2 hashes and the 0925 auction projection/anchor against the DB5
baseline, while 0920 and 0924 remained one member short with ranked/summary
differences. Thus Q2 source alignment is supported, but barrier-time auction
parity remains open. Keep Phase P `PARTIAL`; the next diagnostic is to compare
which real source members have been consumed at each business barrier, not to
relax completeness or synthesize a member. Details:
`docs/work/handoffs/TD_T1V2_SOURCE_ALIGNMENT_AUDIT_20260925.md` and
`docs/work/handoffs/TD_T1V2_SOURCE_ALIGNED_HYBRID_REPLAY_20260925.md`.

Phase O update: the current state is `PHASE_O_PARTIAL`. The same-day real
2026-09-24 `09:15:00–09:40:00` TD window was replayed through the exact
t1-v2 release into isolated Redis DB9/`task009o:` with TD writes disabled.
The independent TD count and t1-v2 `source_in/ticks` were both `1,194,572`;
the run produced 1,224 batches, 78 clocks, 2,408,279 Redis commands,
`td_sql=0`, and `ack=0`. Core read 5,222 Q2 hashes with coverage `1.0`,
ordered/shuffled projection equality, and equal sampled Engine hashes. Under
the explicit 10-second freshness policy 116 quotes were stale, so the result
is `REPLAY_PARTIAL` and `normal_opening_pass=UNPROVEN`. The stable three-field
0920/0924/0925 legacy auction projections matched prior isolated runs. DB0 had
zero `task009o:*` keys. Evidence:
`docs/work/handoffs/TD_RABBIT_PHASE_O_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-o-0924-full-20260924T224351+0800/`.
This closes another real TD→t1-v2→isolated Redis→Core full-window check, but
does not prove Rabbit delivery order, historical `available_at`, live
09:25:06 visibility, or NORMAL acceptance.

Phase N update: the current state is `PHASE_N_PARTIAL`. The same-day real
2026-09-24 `09:15:00–09:32:09` TD→exact t1-v2→isolated Redis DB7→Core
opening bridge completed; t1-v2 processed 429392 ticks in 753 batches with
78 clocks, `td_sql=0`, `ack=0`, and 878448 Redis commands. Core read 5222
Q2 hashes directly from the isolated prefix at the historical 09:32:09
observation, with coverage 1.0, 24 stale quotes under the explicit 10-second
policy, and equal ordered/shuffled plus sampled Engine hashes. The result is
`REPLAY_PARTIAL`; NORMAL opening and historical `available_at` remain
UNPROVEN. Evidence: `docs/work/handoffs/TD_RABBIT_PHASE_N_AUDIT_20260924.md`.
The same window was repeated into isolated DB8/`task009n2:` with identical
429392/753/78/878448/`td_sql=0`/`ack=0` counters and zero normalized semantic
key differences (SHA
`c88639da305a303221c8ea9ca900060a816551bcf0385244aadb62f854050a05`).
An ECC contract-first audit also confirmed that the exact wire schema has
`DataBatch.batch_id/records/sent_at`, while the current C++ runtime `TickBatch`
does not propagate `batch_id`, `sent_at`, or `record_count`; per-tick arrival,
delivery sequence and completion watermark are absent. This is recorded in
`docs/work/handoffs/TD_RABBIT_CONTRACT_AUDIT_20260924.md` and is a future
approved contract task, not an implicit production change.
Phase M update: the current state is `PHASE_M_PARTIAL`. A same-day real
2026-09-24 `09:15:00–09:25:09` TD→t1-v2→isolated Redis→Core bridge completed
without TD writes or Rabbit ACK; see
`docs/work/handoffs/TD_RABBIT_PHASE_M_AUDIT_20260924.md`. The longer historical
status line below is retained as a phase narrative; Phase M is the latest
decision point.
The same cutoff was repeated in a second isolated Redis DB and its normalized
semantic content matched; production DB0 remained untouched.
The 0925 anchor was then compared against all 5222 same-day TD snapshot rows:
5208/5208 comparable match and rest-bid values matched, while 14 rows were
explicitly unavailable (NULL price/change and zero amounts). Rest-ask/price/
limit are not carried by that anchor and remain not comparable.
旧 exact-release/validation reader 按历史调度行为需要延伸到 09:25:09 才能
查询屏障后的空片。当前开发 reader 已将片右边界屏障纳入该片处理计划：
`[09:25:03,09:25:06)` 可在结束时发出 09:25:06 Clock，因此 `end=09:25:06`
不需要再查询 `[09:25:06,09:25:09)` 才能冻结。若继续运行至 09:25:09，随后
空片仍会推进 Q2/latest 观察时间，但不产生新 tick。历史行为保留在对应旧审计。
Read-only TD search across 2026-09-18–2026-09-24 found no rows in the 06–09
second interval on any date, so mixed 05/06/07 evidence remains dependent on a
real Rabbit capture or future source data.

状态：`PHASE_L_PARTIAL`，2026-09-24 更新。Phase A 已完成只读源合同/当前发布包基线；Phase B 已用真实 TD 完成完整 500 片 sliced-read 验证，并在 canonical ordering 修复后重新验证：500 次逐片查询、1,224,811 行、98 空片、时钟推进到 09:40。Phase C 已完成真实 TD 09:25:00–09:25:06 bounded 桥接和 09:15–09:40 全窗口 t1-v2 Q2Frame 桥接，并由 Core 对冻结 Q2Frame 双次回放验证确定性。Phase E/F 已在隔离 Redis 完成 09:25:06 空片 Clock、0924/0925 跨屏障闭环；Phase G 又在更长的 09:20–09:25:09 窗口真实写入隔离 Redis并由 Core 读回验证；Phase I 在 2026-09-23 09:24–09:25:09 以当前 t1-v2 发布包和两个隔离 Redis DB 做跨交易日重复验证；Phase J 证明 09:20 起始仍遗漏 16 只只在 09:15 有基线的股票；Phase K 以 09:15–09:25:09 完整真实窗口重放后，5222 个 TD/Q2/0925 快照股票集合完全一致，并在 DB5/DB4 重复验证语义 hash。进一步核对发现 A2/anchor 缺少的 14 个 symbol 在旧 0925 快照中全部是 `px/chg=NULL`、`match/rest=0` 的不可用事实行，故不应当作 Q2 丢失；其余 5208 个 A2 symbol 的 chg、match、rest bid/ask 全部一致，5068 个可比较价格全部一致，140 个价格 NULL 行显式保留 unavailable，整体仍为 `PARTIAL_WITH_EXPLICIT_UNAVAILABLE`。Phase L 又对同日完整 09:15–09:40 真实窗口直接写入隔离 Redis DB3/DB2，两次均为 1,204,178 ticks、1,224 batches、78 clocks、2,430,075 Redis commands，规范化语义完全一致；0920/0924/0925 冻结键与 09:25 截止运行完全相同，证明未被全天滚动 Q2 覆盖；最终 5222 个 Q2 capture 交给 Core 后，coverage=1.0、ordered/shuffled projection hash 相同、抽样 Engine hash 相同，Core 结果为 `REPLAY_PARTIAL`（68 个 stale），不是正常门禁通过。TD 写入关闭、Rabbit/ACK 未发生。Rabbit delivery 等价、live 可见集合和 historical available_at 仍未证明，正常开盘 acceptance 不变。当前生产日志曾出现 TD `No enough disk space` 警告，Rabbit 历史捕获仍缺失；不表示任何生产等价或下一阶段通过。`docs/work/CURRENT_TASK.md` 的 TASK-008 状态不因本文改变；M3-1 的独立生产门禁也不因历史回放放宽。证据：`/home/exedev/validation/td-rabbit-phase-a-20260924/phase_a_report.md`、`/home/exedev/validation/td-rabbit-phase-b-slice-20260924T012653+0800/phase_b_report.md`、`/home/exedev/validation/td-rabbit-phase-b-full-canonical-20260924T192010+0800/phase_b_canonical_report.md`、`/home/exedev/validation/td-rabbit-phase-c-q2-2slice-20260924T014004+0800/phase_c_report.md`、`/home/exedev/validation/td-rabbit-phase-c-q2-full-20260924T014403+0800/phase_c_full_report.md`、`/home/exedev/validation/td-rabbit-phase-e-redis-20260924T193920+0800-barrier/phase_e_report.md`、`/home/exedev/validation/td-rabbit-phase-f-0924-0925-20260924T194854+0800/phase_f_report.md`、`/home/exedev/validation/td-rabbit-phase-g-0920-0925-20260924T200214940+0800/redis_repeat_comparison.json`、`docs/work/handoffs/TD_RABBIT_PHASE_I_AUDIT_20260924.md`、`docs/work/handoffs/TD_RABBIT_PHASE_K_AUDIT_20260924.md`、`docs/work/handoffs/TD_RABBIT_PHASE_L_AUDIT_20260924.md`。

本计划把 **t1-v2 源快照冻结**固定为 09:25:06 时钟屏障，不以等齐股票作为触发条件。这是对 `docs/REAL_DATA_MIGRATION_PLAN.md` 中较早“09:25:06 仅最早检查、可等到 09:25:10/30”的目标语义修订；旧文字保留为历史阶段记录。`engine-next` 消费门禁与生产现状须另行核对，本计划不自动改动或部署它们。

补充的真实 live-log 审计见 `docs/work/handoffs/TD_RABBIT_LIVE_LOG_AUDIT_20260924.md`：
日志能证明 progress/ACK/last_ts_ms，但没有 delivery membership、arrival
order 或 completion watermark，因此不能把生产 progress 当作 Rabbit arrival
等价证据。

## 不可偏离的主线

```text
生产 Rabbit DataBatch ──┐
                       ├─ 同一 RawTick/TickBatch → 同一 t1-v2 Q2/竞价计算
TD 只读、逐个 3 秒片 ────┘
                              ├─ 实盘：既有 Redis/TD owner 与 ACK 不变
                              └─ 回放：允许写隔离 Redis/Q2 证据，不写 TD、不消费 Rabbit
```

- 回放每次只读取一个全市场 `[start_ms,end_ms)` 3 秒片，保留片内**每一条**真实 tick；不一次装载全天，不把片再拆成计算 `chunk`。允许至多一个片的有界预读。空片也推进时钟，但不制造 tick。
- TD 片是一次**读取/时间线边界**，转换成 Rabbit 同字段的 tick；它不是已证明与生产 Rabbit delivery 相同的**处理批次**。用户报告 Rabbit 可能将一个时间片分成 3–4 个 delivery，且一个 delivery 可混有相邻事件秒；该固定数量尚无逐 delivery capture 验证。2026-09-24 09:15:01–09:39:58 live progress 累计计数显示 3,296 个已处理/ACK batch、1,930,517 条 source record，约 585.7 条/batch、约 6.6 个 batch/名义 3 秒；这只是区间计数率，不是 delivery→frame 映射。详见 `docs/work/handoffs/TD_RABBIT_LIVE_LOG_AUDIT_20260924.md`。片内必要的时钟屏障只划分处理先后，不重新查询 TD、不丢 tick，也不引入行数/性能 `chunk`。字段同形、逐 tick 计算相同和不同分批下状态相同是三项分别验收的结论；真实 delivery 边界、到达顺序及历史 `available_at` 未记录时保持 `UNKNOWN`。
- 每条 tick 都进入 t1-v2 Q2 状态机；选竞价候选只是额外的逐股视图，**不得**预先去重而改变累计量差分、盘口更新或 Q2。实盘 Q2 逐 tick 即时更新，不等待 3 秒片。
- Q2 数值计算不以程序启动时刻或交易时段作准入门禁；竞价/连续阶段的**事实计算规则**仍需区分。当前 `EngineCore::on_batch` 以整批 `logical_ts_ms` 决定阶段，TD 片的结束时间不一定适用于片内每条 tick；09:25/09:26/09:30 等跨界片必须先做同输入、不同分批与阶段归属差异测试，不能仅凭原始 tick 数相等宣布行为等价。
- Q2 的逐股最新值会被后续行情覆盖；09:25 锚点必须另存冻结版本，不能在 09:30 回头扫描滚动 `q2:<symbol>` 拼接 09:25。回放只使用隔离 Redis namespace/DB；不写 TD，也不覆盖生产 key。
- 09:20/09:24 缺失不阻止 09:25 分析；缺失的相邻 delta 保持 `UNKNOWN`。数据不齐降低事实质量，不默认停机；日期、schema、单位、时钟倒退及副作用边界仍是硬约束。

## 已核对的实现事实与真实数据（不是通用市场定律）

- 当前生产发布包的 `QuoteStateStore` 已按 symbol 保存状态并预留约 6000 个位置；`AuctionState` 已有 a20/a24/a25 价格字段。先复用现有逐股状态，不预建第二套全市场“席位”。现有 `QuoteCalculator` 明确区分：竞价时一级价格、二级未匹配量；连续交易时一级盘口的含义不同。`AuctionCalculator` 在 09:25 前优先使用一级撮合价，之后可使用成交价。参考生产发布包 `C/t1_v2/{quote_state_store,auction_state,quote_calculator,auction_calculator}.cpp/.h`，实施时再次核对正在运行的 exact release，不能只看开发工作树。
- TD 客户端本次按 UTC 解析/显示 SQL 时间：下面上海时间 `09:25` 的查询边界为 UTC `01:25`。`auction_snapshot_v2.ts=09:25:06.197` 是快照写入时间，不是所有个股的原始 tick 时间。
- 2026-09-18：`[09:24:57,09:25:00)` 有 4873 条 tick，二档/五档价格展开数均为 0；`[09:25:00,09:25:03)` 有 5092 条，二档/五档展开 4222；`[09:25:03,09:25:06)` 有 949 条，全部展开。合并的 6041 条覆盖 5171 个 symbol；每个 symbol 至少有一条买侧**或**卖侧第五档有效的结束盘口候选。09:25 快照有 5221 个 symbol，其余 50 个在这六秒没有新 tick：38 个在前六秒可见，12 个在前六秒仍未见，不得把它们伪造成 09:25 新 tick。
- 用“09:25 窗口内逐股最新的单侧或双侧五档候选”对照 2026-09-18 0925 TD 快照：候选覆盖 5171/5221；在快照价格非 NULL 的 4408 只中，候选 `px_milli` 与快照价格 4408/4408 一致。另 763 只的历史快照价格为 NULL，虽然原始候选价格非零；这部分是待解释的生产投影差异，**不能**算作价格 parity PASS。原始 `amt_yuan` 与派生 `match_amt_yuan` 有 1579 只不相等，二者不是可直接宣称等价的同字段，必须经同版 t1-v2 计算链复核。
- 2026-09-18 的 6041 条中，5159 条双侧第五档有效、8 条仅买侧有效、4 条仅卖侧有效、870 条两侧都未展开。快照中的 7 只涨停、2 只跌停均属于这 12 条单侧盘口；另 3 条单侧盘口的快照 `limit_state=Normal`。因此单侧五档是**有效结束盘口形态**，但不是涨跌停判定。不得要求买卖两侧五档都齐，也不得把一侧为 0 解释为字段缺失。
- 2026-09-23：`[09:24:57,09:25:00)` 4898 条均无第五档展开；`[09:25:00,09:25:06)` 5072 条中 5056 双侧、6 仅买侧、6 仅卖侧、4 两侧未展开。后 4 只在 09:25:02/03 才出现展开后的真实 tick，说明不能将 09:25:00 首个观察直接定稿。该日尚未完成与快照的全字段逐股比对，不宣称两日全量 parity。
- 2026-09-18 `[09:25:06,09:30:00)` 原始 TD tick 为 0；空窗必须由独立时钟事件触发 09:25:06 冻结，不能等待下一条 tick。09:30 后多档盘口同样可能完整展开，故五档识别必须受竞价时间/阶段约束，不能脱离时间独立判别。
- 当前生产发布包的 `EngineCore::on_batch` 先 `advance_clock(batch.logical_ts_ms)`、再遍历所有 tick；`RuntimePipeline` 在计算后根据触发标记构造 Redis/TD 命令。live loop 每约 250 ms 有独立时钟，且对 09:32:10 有混合 batch 显式拆分，但 09:25:06 尚无同样的拆分。故跨 09:25:06 的一个 delivery 是否把之后的 tick 带入冻结状态，不能仅由 `SnapshotTrigger` 的阈值推断；阶段 B/E 必须单独检查和复现。

这些数字来自本次对 `market_data1.stock_tick_v2` 与 `market_data1.auction_snapshot_v2` 的只读 `SELECT`、逐股内存比较；未写源库或服务。它们证明上述日期的盘口形态与部分字段对齐，不证明真实 Rabbit 到达先后、09:25:06 实盘可见集合或当前发布包与历史写入版本的完全等价。

## 最小候选与冻结规则（待实现、待审计）

1. **输入与 Q2**：同一适配后的 `RawTick/TickBatch` 进入同一 t1-v2 Q2/竞价函数；每条真实 tick 都参与计算。TD 只负责按 3 秒片只读供给，不在 TD reader 里算 Q2 或执行行情时机判断。用同一批真实 tick 分别按“单个全市场片”和“若干模拟 delivery”处理，比较逐股 Q2、竞价状态、阶段和最终 hash；差异必须定位为 batch/time 语义，不静默归为数据缺失。模拟 delivery 不得冒充真实 Rabbit arrival 证据。
2. **逐股“时间打擂”**：在 09:25 竞价窗口内，将 `bp5_milli>0 OR ap5_milli>0` 作为真实数据已支持的“结束盘口候选”形态。每只股票保留当时**已处理**候选中源时间较新的一个；同源时间、不同内容且无真实 sequence 时记录 `ORDER_AMBIGUOUS`，不得用合成排序声称实盘先后。候选的源时间胜出规则与 Q2 逐条处理状态是两件事：若迟到的旧时间 tick 会使滚动 Q2 回退，必须显式记录并验证影响，不能假设候选正确就表示 Q2 正确，也不能为解决该问题预先丢掉真实 tick。形态规则为候选筛选，不是交易所提供的最终标记；跨日期、其他市场和无第五档的反例继续验证。
3. **单边与涨跌停分离**：买侧或卖侧单边五档均可胜出。`limit_state` 独立依据可靠的当日限制价、价格方向及对应封单量计算；单边形态不能直接推出涨停/跌停。ST、不同板块、新股/无涨跌幅限制、停牌及 source 限制价元数据缺失时保留 `UNKNOWN/UNPROVEN`，不得仅凭固定 10%/20%/30% 猜测。
4. **09:25:06 屏障**：实盘由墙钟冻结**触发前实际已处理**的状态，不等齐全部股票；回放使用独立虚拟时钟，空片也推进。回放的事件时间重建先处理源时刻截断到 `09:25:06` 的真实 tick（含 `06.xxx`，保留原始毫秒证据），再冻结，最后处理 `07` 秒及之后的 tick；这是同一个 3 秒片内部的时钟屏障，**不是**再次分片查询或 `chunk`。必须测试同一批内同时含 `05.xxx/06.xxx/07.xxx` 的情况，确保回放锚点不包含 `07.xxx`；现有生产 09:25 batch 路径是否具有同样屏障仍待核对。`06.xxx` 是否在实盘冻结前已到达，TD 事件时间无法证明；回放只能标 `EVENT_TIME_RECONSTRUCTION`，不能称 Rabbit arrival parity。
5. **冻结输出**：以当前 t1-v2 内存 `QuoteStateStore` 的已处理状态作为候选/快照计算输入，Redis 是投影输出，不在触发后扫描滚动 `q2:<symbol>` 当历史权威。保存独立、不可被后续滚动 Q2 覆盖的 0925 锚点，记录每股候选源时间、盘口形态、覆盖/缺失数、冻结时钟、实际观察/提交时间与 Redis 提交结果。内存态和已提交滚动 Q2 因节流/写失败不一致时分别报告；快照提交失败不得标为“已发布”。未胜出的股票仍可保留先前观察事实，但标为“结束盘口未确认”；不伪造完整五档或零值。首阶段不引入通用 revision/恢复引擎，真实晚到修正需求经独立证据再审批。

### 分层验收，不用一个好看的比率代替整条链

- `CANDIDATE_SELECTION`：比较逐股候选集合、源时间、双侧/单侧/未展开形态和可比价格；09-18 的 5171/5221 覆盖与 4408/4408 可比价格一致只是这一层的现有基线。无新 tick 的 50 只和快照价格为 NULL 的 763 只单列原因，不算作自动失败或自动成功。
- `Q2_DERIVATION`：同输入、同版本 t1-v2 对照逐股 Q2 数值和来源时间，逐项检查 1579 只原始金额与派生匹配金额差异；不同字段口径不得硬比，也不得用零填充。若历史生产版本/输入不足，报告 `PARTIAL/UNPROVEN`，不阻断其余只读核验。
- `SNAPSHOT_PROJECTION`：核对内存候选/状态、生成的冻结锚点与已提交 Redis/TD 旧快照中**确实同口径**的字段；注明旧快照生产版本是否可证。同一字段不一致才归为 mismatch；历史 NULL、版本不明、Redis 提交不可证均独立计数。前两层通过不自动升级为完整快照或实盘等价。

### Phase G 后续事实：旧快照与事件时间集合的可见性差异

对真实 2026-09-18 `09:20:00–09:25:06.197` 的 129281 条 TD tick，按当前
t1-v2 `AuctionCalculator` 逐条重算后发现，0925 `auction_snapshot_v2` 的
5221 行中有 5184 行能与某一条历史 TD tick 的 `match/br/ar` 完全对应，但只有
3576 行对应 barrier 前按事件时间排序的最后一条 tick。完全对应的历史源时刻
主要集中在 09:25:00、09:24:59、09:25:01 和 09:25:02。这把原先的
“字段公式不一致”假设收窄为“旧快照生成时的可见/到达集合与 TD 事件时间回放
集合不同”的待证假设；它不提供 Rabbit arrival 或 historical `available_at`
证据。回放不能反向选用旧快照值伪造到达顺序，仍须保持 `UNKNOWN/UNPROVEN`。
证据：`/home/exedev/validation/td-rabbit-phase-h-snapshot-source-20260924T200951431+0800/`。

## 分阶段实施与退出条件

| 阶段 | 只做什么 | 退出证据与审计点 |
| --- | --- | --- |
| A. 基线/源合同 | 核对**当前运行 release** 的 Rabbit `DataBatch` 字段、Q2 计算/Redis 节流、整批阶段归属、09:25 触发与快照写入顺序；只读补查 09-18/09-23 TD 和**现成**真实 Rabbit 捕获/日志，不新增 consumer | 字段、单位、源时间、batch/arrival 是否真实可得的矩阵；“3–4 delivery”若无捕获只记为待证描述；内存 `QuoteStateStore` 为冻结计算输入、Redis 为输出的现状和失败路径明确；审计与用户对齐后才进入 B |
| B. 3 秒 TD 供给 | 一次 SELECT 一个半开全市场片；保留全部 tick、空片时钟，最多预读一片；映射为同字段输入，不用 `chunk` 计算路径 | 片边界/数量/内存上界、实际 500 片与空窗证据；Q2 输入 tick 数等于源返回数。用相同真实 tick 重排为一个片与多个模拟 delivery，对照 09:25/09:26/09:30 的阶段、Q2 和竞价状态；不同即报告差异，不宣称实盘 batch parity。不写 TD、不触碰 Rabbit/ACK；审计后进入 C |
| C. Q2 同源计算 | 实盘即时 Q2 不变；回放在隔离 Redis 或 dry-run 中复用 t1-v2 的同一计算/投影；另外核对迟到旧时间 tick 是否使滚动状态回退 | 同输入重复回放 Q2 hash、逐股来源时间和计数一致；内存态、生成命令、已提交 Redis 态和提交失败分别可观察；Q2 计算不受启动时刻门禁；不把 Core 自算 Q2 冒充 t1-v2；审计后进入 D |
| D. 五档候选 | 在既有逐股状态增加最小候选，不额外预分配全市场第二份席位；单侧或双侧第五档均可入选，Q2 仍吃全部 tick | 输出 `CANDIDATE_SELECTION` 独立结论：09-18 逐股 5171 候选、50 无当秒新 tick、4408 可比价格；09-23 的 4 只早期未展开→后到展开和涨跌停/普通单侧回归。763 NULL 与 1579 金额差异分别移交派生/投影层，不能以价格命中率宣称整体通过；审计后进入 E |
| E. 冻结与 Redis | 独立 09:25:06 clock barrier；内存状态冻结为单独锚点，滚动 Q2 继续；回放仅隔离 Redis，TD 零写入 | `05/06/07` 混合时间 batch、空片、09:30 后继续流和 Redis 写失败测试；锚点不被 07/30 秒行情覆盖、无“等齐全市场”硬停机；实盘墙钟与回放事件时间重建的差异明确，不假装 live 可见性；审计后进入 F |
| F. 端到端真实验证 | 同一天同输入做 TD→t1-v2→隔离 Redis→锚点→Core 只读事实，按天/逐股对照既有快照的可比字段 | `CANDIDATE_SELECTION`、`Q2_DERIVATION`、`SNAPSHOT_PROJECTION` 三份结论与两次重复 hash、coverage/缺失/例外、代码版本/副作用审计；tester/auditor 独立复核。缺历史版本或 arrival 时保持 `PARTIAL/UNPROVEN`，不伪报完整等价；仅此时讨论下一阶段，不自动部署生产 |

若阶段 B 发现整批 `logical_ts_ms` 让边界前 tick 被归到边界后阶段，优先在**同一已读取片内部**按 tick 源时间切换计算阶段/插入时钟屏障，再用相同输入复核；不增加 TD 查询、不预丢 tick，也不冒充 Rabbit 原始 delivery。若差异仅来自每批 Redis flush/节流，则分开报告 Q2 内存计算与存储提交时序，不为了做出相同 hash 编造生产批次。任何改法都须先独立验证，再决定是否进入下一阶段。

每阶段结束都要做一次**主线对齐审计**：是否仍为 Rabbit 同形输入、单片供给、同一 t1-v2 Q2、实盘即时/回放隔离、09:25:06 冻结；记录 `PASS/PARTIAL/BLOCKED`、真实证据和未知项，再由用户决定是否推进。Phase C 审计记录在 `docs/work/handoffs/TD_RABBIT_PHASE_C_AUDIT_20260924.md`，Phase E 审计记录在 `docs/work/handoffs/TD_RABBIT_PHASE_E_AUDIT_20260924.md`，Phase F 审计记录在 `docs/work/handoffs/TD_RABBIT_PHASE_F_AUDIT_20260924.md`；Phase F 仍为 `PARTIAL`，不自动进入下一迁移阶段。性能只记录实测耗时与瓶颈，不因一个武断阈值把正确回放判失败；若一个局部问题连续两轮证据驱动修复仍不收敛，应暂停该局部、提出假设和取证办法，不无限循环。

## 必须保持的边界

- 本计划允许在明确批准的独占 Redis DB/唯一 namespace 中写入回放证据；禁止使用生产 DB0 或生产 key 前缀。仍不授权修改生产 `engine-next`、`t1-v2-live`、Rabbit consumer/ACK、TD writer，也不授权重启或上线。生产写入/部署另行审批。
- 仅日期/schema/单位/未来时间/副作用等安全违规硬失败；缺 0920/0924/个股候选可 `PARTIAL`，不补造。`Missing != Zero`；TopN 不能冒充全市场。
- 历史 TD 当前可查询不等于当时 `available_at`；按事件时间重建的稳定 hash 不等于生产 Rabbit 到达、batch membership 或 09:25:06 实盘快照等价。
- 2026-09-24 收尾复核根分区约 44% 使用率、约 22G 可用；上一阶段曾发生
  空间告警并已单独记录。空间恢复不等于 TD 写入健康证明；不得据此把
  `TD_WRITE_HEALTH` 升级为 `PROVEN`，也不得擅自清理 TD 数据/retention。
