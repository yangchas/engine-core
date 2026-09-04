# Project Knowledge

## 1. Market & Auction Knowledge

- [UNKNOWN] Q2 generation 是否是旧生产链真实提供的全局一致版本，待 Gate K 证实。
- [OBSERVED] 旧 Q2 读取使用 q2:active:{trade_date} 与 q2:{symbol}；px/pc 为 milli 价格字段，其余数量单位必须继续审计。
- [INFERRED] Q2 是 Redis 中各股票最近状态的投影集合，不应默认解释为同一市场时刻的全市场快照。
- [VERIFIED] 2026-09-04 对 cobra-ion 的只读 Probe 可通过既有 server venv 访问 Redis Q2；当时最新可用 cohort 为 `q2:active:20260903`，共 5217 个 symbol。
- [OBSERVED] 2026-09-04 读取 `q2:active:20260904` 时覆盖完整；较早的 `q2:active:20260903` 中大多数记录的 source `ts` 已跨到 2026-09-04，active cohort 不能视为不可变历史快照。
- [UNKNOWN] Q2 `ts` 的精确定义及跨交易日更新顺序仍未由 producer/consumer 证据确认；当前 live check 观察到新 cohort 的 source timestamp 可能统一落在当日午夜。
- [VERIFIED] `C/t1_v2` producer 维护 `amt` 为累计元、`vol` 为累计股数，`amt2m/amt5m` 为累计金额差；Redis/TD 写入保持这些整数单位。
- [VERIFIED] `C/t1_v2` 竞价计算将 `br/ar` 定义为二档价格×二档股数换算的元金额，`am` 为竞价成交金额；它们是派生盘口/成交代理，不是真实净流入。
- [OBSERVED] Q2 producer 写入字段包括 `px/pc/amt/vol/iv/ia/ln/ts/ph/ls/mx/mn/spd1m/amt2m/amt5m/vec3m/vec5m` 以及竞价字段 `a20/a24/a25/am/br/ar` 和 `mk`；只有当前 Wheel 使用的核心字段才冻结到 canonical model。

## 2. Data Contracts

- [VERIFIED] 当前行情、盘口、累计成交和 Q2 freshness 属于实时状态，不应通过查询型数据函数读取。
- [VERIFIED] 历史日线、昨日数据、主题成分、历史 Tick 和参考价格属于查询型数据。
- [VERIFIED] Missing != Zero。
- [VERIFIED] fallback source != fallback semantic。
- [VERIFIED] 已验证连接方式优先复用；当前 engine_core 只抽取薄边界，不重新建设 Redis/TD/网络访问层级。

## 3. Runtime Timeline

- [VERIFIED] 当前已验证的输入是 Q2 projection snapshot fixture/live adapter 和 fixture fact slice。
- [UNKNOWN] Q2Frame、TD event-time replay adapter 尚未实现，接入后再记录其可证明的排序和等价能力。
- [VERIFIED] 所有窗口使用半开区间 [start_inclusive, end_exclusive)。
- [VERIFIED] Timer 与行情输入分离。
- [VERIFIED] 重启跨过节点时使用 RECOVERY_CATCHUP，不伪装为正常准时执行。

## 4. System Capabilities & Fact Authority

- [VERIFIED] 旧 RabbitMQ -> t1_v2 -> TD/Redis -> ACK 链路属于外部成熟生产链，engine_core 首期不接管。
- [VERIFIED] Redis Q2 是运行时投影，不是历史事实权威。
- [UNKNOWN] TD 生产版本、重复写语义和历史输入排序能力待 Gate K 验证。
- [VERIFIED] cobra-ion 上 TD `market_data1.stock_tick_v2` 可由既有 taos 客户端只读访问；2026-09-03 09:20-09:24 查询返回 82183 行，查询排序为事件时间/代码顺序，不代表 Rabbit arrival order。
- [UNKNOWN] cobra-ion `daily_kline.volume` 的零值语义；Probe 样本为 0，不能直接当作 verified zero。
- [VERIFIED] CurrentMarketState 只保存当前可观测数据、轻量 projection 和窗口原始累计状态。
- [VERIFIED] 首个 SegmentFrame 事实切片仅支持 SYMBOL 范围；其他范围显式返回 UNAVAILABLE，不伪装成聚合结果。
- [VERIFIED] SegmentFrame 的 Price/Volume/OrderBook/Breadth/Theme 各自维护状态；未知累计量语义不计算 delta，返回 UNAVAILABLE。
- [VERIFIED] directional_pressure 仅是 Q2 盘口字段差值代理，不得命名为真实资金净流入。
- [VERIFIED] 基础轮子可脱离 Engine 单独运行：Contract、Q2、Window、TemporalDataGuard、SegmentFrame 与相邻段比较均有单项测试。
- [VERIFIED] 当前基础轮子在 cobra-ion 的 Python 3.12.3 server venv 中以临时验证副本运行通过（48 tests passed）；这不是生产部署。

## 5. Replay Capabilities

- [VERIFIED] 当前只完成 Q2 projection fixture/live slice；不支持 REPLAY_RECORDED，也不宣称 Rabbit arrival/batch 等价。
- [UNKNOWN] Q2Frame sequence replay、TD event-time replay 的输入保真度和适配器尚待实现验证。
- [VERIFIED] 不同信息粒度使用 EXACT_EQUIVALENCE 或 SHARED_FACT_EQUIVALENCE。
- [VERIFIED] replay 默认 deny-all effect，并通过 knowledge_as_of 防止未来数据穿越。
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
- Runtime/Replay 统一经过 `TemporalDataGuard`；无法证明 `available_at <= knowledge_as_of` 的数据只用于 oracle、Contract 验证或 fixture capture。

不要：
- 从当前网络查询结果反灌历史 replay runtime。

Evidence：
- `docs/evidence/real_data_probe/20260904T082940+0800/provider_summary.md`

### PITFALL: 重新发明已验证的连接方式

问题：
- 新 Provider 若绕过旧系统的 host、key、session、查询和错误处理，会得到与生产链不同的数据能力。

正确做法：
- 先做 Legacy Connectivity Inventory，只对当前 Wheel 需要的源采用 KEEP/EXTRACT/WRAP；Provider 保持薄边界。

不要：
- 第一阶段建设通用 `RedisAccess/TDAccess/NetworkAccess` 层级。

Evidence：
- `docs/evidence/foundation_audit.md`
