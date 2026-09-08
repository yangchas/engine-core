# Project Knowledge

## 1. Market & Auction Knowledge

- [UNKNOWN] Q2 generation 是否是旧生产链真实提供的全局一致版本，待 Gate K 证实。
- [OBSERVED] 旧 Q2 读取使用 q2:active:{trade_date} 与 q2:{symbol}；px/pc 为 milli 价格字段，其余数量单位必须继续审计。
- [INFERRED] Q2 是 Redis 中各股票最近状态的投影集合，不应默认解释为同一市场时刻的全市场快照。
- [VERIFIED] 2026-09-04 对 cobra-ion 的只读 Probe 可通过既有 server venv 访问 Redis Q2；当时最新可用 cohort 为 `q2:active:20260903`，共 5217 个 symbol。
- [OBSERVED] 2026-09-04 读取 `q2:active:20260904` 时覆盖完整；较早的 `q2:active:20260903` 中大多数记录的 source `ts` 已跨到 2026-09-04，active cohort 不能视为不可变历史快照。
- [OBSERVED] Q2 `ts` 与 cobra-ion 上同一时段 `stock_tick_v2.ts` 对齐，当前 canonical 名称为 `source_record_time_ms`；可以用于 freshness 和 trade-date sanity，但不能宣称为交易所逐笔时间或 Rabbit arrival time。上游供应商对该时间的更细定义仍 UNKNOWN。
- [VERIFIED] `C/t1_v2` producer 维护 `amt` 为累计元、`vol` 为累计手（board lots），`amt2m/amt5m` 为累计金额差；Redis/TD 写入保持这些整数单位。旧注释中的 shares 表述不作为新契约依据。
- [VERIFIED] cobra-ion 当前 `C/t1_v2` 竞价计算将 `br/ar` 定义为一级价格×二档手数换算的元金额，`am` 为竞价成交金额；它们是派生盘口/成交代理，不是真实净流入。
- [OBSERVED] Q2 producer 写入字段包括 `px/pc/amt/vol/iv/ia/ln/ts/ph/ls/mx/mn/spd1m/amt2m/amt5m/vec3m/vec5m` 以及竞价字段 `a20/a24/a25/am/br/ar` 和 `mk`；只有当前 Wheel 使用的核心字段才冻结到 canonical model。

## 2. Data Contracts

- [VERIFIED] 当前行情、盘口、累计成交和 Q2 freshness 属于实时状态，不应通过查询型数据函数读取。
- [VERIFIED] 历史日线、昨日数据、主题成分、历史 Tick 和参考价格属于查询型数据。
- [VERIFIED] Missing != Zero。
- [VERIFIED] fallback source != fallback semantic。
- [VERIFIED] 已验证连接方式优先复用；当前 engine_core 只抽取薄边界，不重新建设 Redis/TD/网络访问层级。
- [VERIFIED] `TemporalDataGuard` 只使用 `available_at_ms` 判断 knowledge cutoff；`available_at_ms` 未知或晚于 cutoff 的 READY/PARTIAL 数据一律 UNAVAILABLE。`observed_at_ms` 仅用于审计、provenance 和 submission identity，不能替代 available_at。
- [VERIFIED] `DataResult.content_hash` 在语义 payload deep-freeze 后派生，调用方不能手填；`FrozenDataBundle` 同时提供排除提交上下文的 semantic `content_hash` 和包含 evaluation/cutoff/status/observed/available 的 `submission_hash`。
- [VERIFIED] 正式 hash 合同固定为 `SemanticHashV1`、`EvidenceHashV1`、`SubmissionHashV1`；Probe trace 和验证证据必须记录合同版本。
- [VERIFIED] `TradingCalendarSnapshotV1` 是当前日期 authority：BaoStock 只读探查生成离线快照，运行时不联网；declared coverage 与 source guard coverage 分离，快照 semantic hash 与 evidence hash 分离。当前真实 BaoStock fixture 在 2026-12-31 右边界没有 2027 successor guard，`next_trade_day` 对该边界 fail closed，不能用合成日期补齐。
- [VERIFIED] `PreviousDayStatsFunction` 只接受交易日请求，并由版本化 CalendarSnapshot 唯一派生 `previous_trade_date`；调用方不能通过 DataContext 注入日期。
- [VERIFIED] `ReadyDataStore` 是最小进程内 readiness 字典，按 function/date/symbol scope/content hash 保存 READY `DataResult`，读取时重新经过 `TemporalDataGuard`；它不是 DataCatalog、Registry 或持久化 checkpoint。

## 3. Runtime Timeline

- [VERIFIED] 当前已验证的输入是 Q2 projection snapshot fixture/live adapter 和 fixture fact slice。
- [VERIFIED] 最小 Q2Frame + VirtualClock replay 已实现：严格校验 `Q2FrameV1` 版本、连续 `seq_no` 和不倒退 `logical_ts_ms`，逐帧生成已有 `MARKET_UPDATE`，不接管 Rabbit 或外部写入。
- [VERIFIED] 所有窗口使用半开区间 [start_inclusive, end_exclusive)。
- [VERIFIED] Timer 与行情输入分离。
- [VERIFIED] 重启跨过节点时使用 RECOVERY_CATCHUP，不伪装为正常准时执行。
- [VERIFIED] `DeterministicEngine` 在入队时冻结 signal payload；相同 `signal_id` 的相同内容在有界内存幂等窗口内幂等，冲突内容拒绝。MARKET_UPDATE/PULSE/TIMER 不得倒退 market frontier，旧 evaluation 的 `DATA_READY` 仍可使用原冻结 Snapshot 完成评估；持久化幂等留待 journal/checkpoint 阶段。
- [VERIFIED] `RECOVERY_CATCHUP` 关闭窗口时保留 `origin=RECOVERY_CATCHUP`，窗口 `finality` 仍为 FINAL。
- [VERIFIED] Engine Integration correctness gates passed: same-time ordering, DATA_READY ownership/isolation, PARTIAL propagation, old-evaluation isolation, bounded long-drain state, duplicate/conflicting signal handling and same-time causal generation ordering.
- [VERIFIED] 一个 Engine session 内 evaluation_id 只能注册一次；终态不会重新进入 pending。tombstone 仅保留有界近期分类，驱逐后仍 fail-closed 为 UNKNOWN。
- [VERIFIED] Foundation 600519 Segment A/B/Comparison hashes remain identical when composed through Engine; current Engine is only an orchestration boundary and does not alter fact-wheel semantics.
- [VERIFIED] TD Event-Time Replay 最小适配器按事件时间、代码和保留原始字段内容 hash 稳定排序，按三秒半开 `EVENT_SLICE` 保留逐条 tick，并逐事件提交同一个 Engine；缺少 symbol 时保持 PARTIAL。

## 4. System Capabilities & Fact Authority

- [VERIFIED] 旧 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 链路属于外部成熟生产链，engine_core 首期不接管。
- [VERIFIED] Redis Q2 是运行时投影，不是历史事实权威。
- [UNKNOWN] TD 生产版本、重复写语义和历史输入排序能力待 Gate K 验证。
- [VERIFIED] cobra-ion 上 TD `market_data1.stock_tick_v2` 可由既有 taos 客户端只读访问；2026-09-03 09:20-09:24 查询返回 82183 行，查询排序为事件时间/代码顺序，不代表 Rabbit arrival order。
- [VERIFIED] cobra-ion 上 `auction_snapshot_v2` 存在 2026-09-03 的 09:20:03 与 09:24:10 真实快照；600519 的匹配金额、resting bid/ask 与价格已固化为只读证据，可用于第一条 Segment A/B Golden fixture。
- [UNKNOWN] cobra-ion `daily_kline.volume` 的零值语义；Probe 样本为 0，不能直接当作 verified zero。
- [VERIFIED] CurrentMarketState 只保存当前可观测数据、轻量 projection 和窗口原始累计状态。
- [VERIFIED] 2026-09-04 在 cobra-ion 只读 Redis Q2 子集（64 symbols）已通过当前内存 Engine 完成 `MARKET_UPDATE -> TIMER -> EngineSnapshot -> ProbeStrategy`；该次显式 freshness policy 下 1 条记录 stale，因此 projection 为 PARTIAL，不代表生产默认 freshness。
- [VERIFIED] 首个 SegmentFrame 事实切片仅支持 SYMBOL 范围；其他范围显式返回 UNAVAILABLE，不伪装成聚合结果。
- [VERIFIED] SegmentFrame 的 Price/Volume/OrderBook/Breadth/Theme 各自维护状态；未知累计量语义不计算 delta，返回 UNAVAILABLE。
- [VERIFIED] directional_pressure 仅是 Q2 盘口字段差值代理，不得命名为真实资金净流入。
- [VERIFIED] 基础轮子可脱离 Engine 单独运行：Contract、Q2、Window、TemporalDataGuard、SegmentFrame、相邻段比较、昨日数据行归一化和真实 600519 竞价段 fixture 均有单项测试。
- [VERIFIED] Gate B 已澄清 09:25 的两层边界：cobra-ion t1-v2 在 `09:25:06` 通过 settling barrier 形成源 A25；同一发布包 Python runtime 在 `09:25:10` 前保持等待、之后才发出正式消费事件。二者分别是 source snapshot gate 与 legacy consumer gate；engine_core 不在本阶段自行合并。
- [VERIFIED] 当前真实 600519 竞价事实只纳入 `price_milli`、`auction_amount_yuan`、`auction_bid_amount_yuan`、`auction_ask_amount_yuan`；Segment A `[09:15,09:20)` 为 PARTIAL，Segment B `[09:20,09:24)` 为 READY，主题/市场宽度不进入该最小案例。
- [VERIFIED] 竞价事实比较的最小可迁移对象是 `P/M/RB/RA` 的段间变化；它属于 Fact，不等同于 `turn_strong`、`BUY` 或其他策略结论。
- [VERIFIED] `AuctionFactShadow` 只对同一标的相邻 Segment 输出 P/M/RB/RA/pressure 端点变化和事实比较标签，固定为 `FACT_ONLY/OBSERVE`；它不迁移旧系统的买盘阈值、转强/转弱、撤单或波动策略。
- [VERIFIED] 600519 的 09:20→09:24 相邻竞价事实已通过独立 source-formula 差异测试：`P_delta=-2,060` milli、`M_delta=4,407,516` yuan、压力由 `-129,960` 变为 `648,770`（压力差 `778,730`）；该测试只验证事实轮子，不验证策略阈值。
- [UNKNOWN] 旧系统中分散出现的 `bid_amount > ask_amount * 1.5`、撤单和波动阈值尚未完成当前生产路径、单位、consumer 和状态生命周期的闭环验证，不得直接迁移为正式策略。
- [VERIFIED] 当前基础轮子可在 cobra-ion 的 Python 3.12.3 server venv 临时验证副本中运行；这不是生产部署。
- [VERIFIED] `normalize_previous_day_stats_rows` 是旧日线访问结果的薄纯边界：严格校验六位代码、保留显式零值、拒绝缺失核心字段/重复代码，并输出稳定排序的昨日统计映射；空结果经 Provider 包装后为 MISSING。
- [VERIFIED] Calendar snapshot 的 `completion_cutoff_time` 是调用侧数据完成策略，不是日历 source fact；naive datetime、非交易日请求和超出覆盖范围均 fail closed。
- [VERIFIED] `MinuteWindowTracker` 是独立的分钟窗口轮子：按显式 source epoch 时间和 Asia/Shanghai 分钟桶保存有界累计价格/金额点，1 分钟价格变化要求相邻分钟，2 分钟金额沿旧实现使用前两分钟内最早可用参考点；缺参考或累计值回退不伪造增量，不使用系统当前时间。
- [VERIFIED] `SessionPlanV1` 将“是否交易日”和“交易日内属于哪个阶段”分开：`TradingCalendarSnapshot` 先确认指定日期可交易，计划再用显式 Asia/Shanghai 时区和全天连续半开区间分类；周末/节假日不再因墙钟时刻被误判为 AUCTION/INTRADAY，15:00 明确属于 POSTMARKET。
- [VERIFIED] Q2 rolling fields `spd1m/amt2m/amt5m/vec3m/vec5m` 已进入显式 canonical contract：速度/向量为整数 basis points，金额窗口为整数 yuan；当前生产 `C/t1_v2` 负责计算，`engine_next` opening consumer 实际读取这些字段。它们是窗口事实，不是强弱或资金流结论。
- [VERIFIED] cobra-ion 上 `TDPreviousDayStatsProvider` 已通过既有 taos 只读路径取得 2026-09-03 的 3 行 `daily_kline`；因没有历史 `available_at` 证据，`PreviousDayStatsFunction` 按规则返回 UNAVAILABLE，而不是把查询时刻冒充可用时刻。
- [VERIFIED] TD 昨日数据没有历史 `available_at_ms` 证据时，无论首次查询时刻还是节点前预取，Runtime/Replay 均按 UNKNOWN availability 返回 UNAVAILABLE；只有具备 verified `available_at_ms <= knowledge_as_of_ms` 的结果才可进入 FrozenDataBundle。`observed_at_ms` 仅保留为审计和 submission identity。
- [VERIFIED] cobra-ion live Q2 + TD shadow path completed without writes: Q2 5217/5217 coverage with 10 stale symbols, TD previous-day result UNAVAILABLE due unknown availability, FrozenDataBundle completeness 0.0, SegmentFrame PARTIAL with price/pressure READY, and Probe trace preserved PARTIAL.

## 5. Replay Capabilities

- [VERIFIED] 当前只支持 Q2 projection fixture/live slice、Q2Frame 和 TD event-time replay；不支持 REPLAY_RECORDED，也不宣称 Rabbit arrival/batch 等价。
- [VERIFIED] Q2Frame replay 与同一规范化 Q2 fixture 进入同一 Engine 的 semantic snapshot/Probe 结果 EXACT_EQUIVALENCE 已通过；该结论仅覆盖 Q2Frame 的 logical timestamp/逐帧 projection，不代表 Rabbit arrival/batch 等价。
- [VERIFIED] TD Event-Time Replay 已通过本地与 cobra-ion 只读验证；当前能力是 `DETERMINISTIC_EVENT_TIME_ONLY`，不恢复 Rabbit arrival/batch，不做 watermark 或 late correction。
- [VERIFIED] TD Event-Time Replay 在同一 `event_time + symbol` 且缺少真实 source ordering key 时，使用原始字段内容 hash 作为 deterministic synthetic tie-break；该 hash 不代表生产真实先后。
- [VERIFIED] Replay signal construction 不推进共享 VirtualClock；按 logical time 分组后在 Engine 消费前推进一次，同刻 child signal 进入下一 causal generation，不能越过当前 generation 的 parent/sibling。
- [VERIFIED] 不同信息粒度使用 EXACT_EQUIVALENCE 或 SHARED_FACT_EQUIVALENCE。
- [VERIFIED] replay 默认 deny-all effect，并通过 available_at <= knowledge_as_of 防止未来数据穿越；未知 available_at 不能被 observed_at 推导。
- [VERIFIED] Real Data Probe 只作为字段/连接证据和 fixture capture；当前能查到历史数据不等于历史 `available_at` 已早于 replay 的 `knowledge_as_of`。

## 6. Known Pitfalls

### PITFALL: Q2 projection 被误称为全市场同刻快照

问题：
- Redis 读取的是各股票最近状态，读取时间和 source 更新时间可能不同。

正确做法：
- 记录 observation time、source update range、coverage、consistency_status 和 content hash。

### PITFALL: fallback 改变业务语义

问题：
- 缺昨日数据时错误使用前日数据并返回 READY。

正确做法：
- 只有业务定义等价的来源才能 fallback；实际日期不符返回 STALE。

### PITFALL: async completion order 影响业务顺序

问题：
- 多个 DataFunction 先返回哪个就先构造 Bundle。

正确做法：
- 按 evaluation_id 收集全部 required 结果，再按 DataRequirement 固定顺序构造 FrozenDataBundle。

### PITFALL: EVENT_SLICE 预先丢失 Tick 信息

问题：
- 把三秒 slice 提前压成 OHLC 或 Feature，无法验证五档路径和 first-touch。

正确做法：
- EVENT_SLICE 只是 transport/scheduling batch，保留逐条 Tick 和稳定顺序。

### PITFALL: 端点快照冒充完整时间段路径

问题：
- 只有时间段起止快照时，无法证明段内最高/最低或 first-touch 路径。

正确做法：
- SegmentFrame 仅输出起止可观察事实；high/low、段内路径等字段保持 None，等待逐条事件输入。

Evidence：
- tests/test_facts.py、examples/run_fact_vertical_slice.py
- docs/evidence/ 后续保存 Gate K、Vertical Slice 和 Gate B 原始报告。

### PITFALL: 把已能查询的数据当作历史时点可知

问题：
- 今天的网络或 TD 查询可能返回历史最终值，但不证明交易时点已经可见。

正确做法：
- Runtime/Replay 统一经过 `TemporalDataGuard`；若没有历史 `available_at` 证据，数据一律不能作为该 cutoff 的 runtime input，只能用于 oracle、Contract 验证或 fixture capture。`observed_at` 仅记录实际观察时间，不得替代或推导 `available_at`。

不要：
- 从当前网络查询结果反灌历史 replay runtime。

Evidence：
- `docs/evidence/real_data_probe/20260904T082940+0800/provider_summary.md`

### PITFALL: 节点时刻临时查询参考数据

问题：
- 节点触发时才查询昨日/参考数据，查询观察时间可能已经晚于节点的 `knowledge_as_of`，即使数据业务日期属于昨日也不能安全进入该评估。

正确做法：
- 在节点前通过同一 `DataFunction` 只读预取，经过 `TemporalDataGuard` 后放入最小 `ReadyDataStore`；节点只读取已冻结结果。

不要：
- 为了让 Replay READY 而填写猜测的 `available_at`，或把当天查询时间反推成历史首次发布时间。

Evidence：
- `docs/evidence/reference_data_readiness_20260904.md`

### PITFALL: 重新发明已验证的连接方式

问题：
- 新 Provider 若绕过旧系统的 host、key、session、查询和错误处理，会得到与生产链不同的数据能力。

正确做法：
- 先做 Legacy Connectivity Inventory，只对当前 Wheel 需要的源采用 KEEP/EXTRACT/WRAP；Provider 保持薄边界。

不要：
- 第一阶段建设通用 `RedisAccess/TDAccess/NetworkAccess` 层级。

Evidence：
- `docs/evidence/foundation_audit.md`

### PITFALL: 合成 Tie-break 被误称为生产到达顺序

问题：
- TD Event-Time Replay 可能遇到同一 `event_time`、同一 symbol 且没有 source sequence 的多行事件。

原因：
- 为了让回放可重复，当前实现使用保留原始字段内容 hash 做最后排序键；TD 未保存 RabbitMQ arrival/batch 顺序。

正确做法：
- 将该 hash 明确标为 deterministic synthetic ordering；只用于稳定回放，不用于解释生产先后。
- 未来只有找到真实 row/source sequence 才能替换排序键。

不要：
- 把 TD 查询顺序或 raw hash tie-break 当作生产 arrival order。

Evidence：
- `docs/evidence/td_event_time_replay_20260904.md`
- `docs/evidence/gate_b_auction_open_audit_20260904.md`

### PITFALL: 旧版盘口金额公式与当前发布包不一致

问题：
- 本地旧版 `C/t1_v2` 曾按二档价格×二档手数计算 `br/ar`，cobra-ion 当前发布包已按一级价格×二档手数计算。

正确做法：
- 以当前运行环境的发布源和真实原始行复算 Golden fixture；保留公式修正证据，不把旧派生值直接当作当前契约。

不要：
- 只复用旧 fixture 的派生字段，跳过当前 producer/consumer parity。

Evidence：
- `docs/evidence/gate_b_formula_correction_600519_20260905.md`
