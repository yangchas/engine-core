# 交易日历数据域调研（C325）

## 结论

**已证实**：engine-next `TradeCalendar.get_trade_days_of_month` 当前实际走节假日库估算；紧接着的深交所请求分支因前面的 `return` 不可达。2026 全年实取深交所 12 个月、BaoStock 1 次全年查询，两源均给出 242 个交易日，逐日差异为零。`holidays.CN()` 加工作日规则在此次快照上也恰为 242 天、逐日相同，但它没有市场日历语义保证。Core 的 `trade_dates.json` 是 76 个日期的纯 JSON 数组，范围 `2026-06-26..2026-10-19`；该共同覆盖区间与两源完全一致，文件没有来源、更新时间或版本信息。

**未证实**：深交所接口历史可用性、源数据修订通知/SLA、BaoStock 未发布未来日期的具体策略；guardian 获取其 `calendar_dates` 参数的上游路径。2026-11、12 响应是本次真实响应，不等于未来日程不可修订。

**需用户决策**：确定权威主源（建议沿用 BaoStock 作为 Core 离线采集主源，深交所作为独立校验/备源）、产物发布位置与版本保留策略、更新调度责任人，以及 Core 消费者切换节奏。本文不实施切换。

## 1. 原系统生效行为

- `/home/exedev/services/engine-next/current/web/trade_calendar.py:39-51`：`_build_local_trade_days` 跳过周末和 `holidays.CN()` 命中的日期，返回工作日估算。
- 同文件 `:64-70`：`get_trade_days_of_month` cache miss 时调用本地估算、写缓存并 `return trade_days.copy()`。
- 同文件 `:72-80`：其后才构造 `szse.cn/api/report/.../monthList` 请求并按 `jybz == '1'` 提取 `jyrq`；这是不可达代码。该请求也未设置已验证所需的 UA。故该类当前月份结果不是深交所实时数据。
- `/home/exedev/services/engine-next/current/web/services/trading_calendar_service.py:13-15,29-45`：另一实现只用 `holidays.CN()` 与周末判断；`:51-62` 前一交易日向前最多 30 天，失败返回输入日期。它不查交易所日历，也不能表示调休工作日。
- `market_edge_engine.py` 有多处调用 `self.calendar.get_previous_trade_day` / `is_trade_day`（例如 `:1108`, `:8668`, `:9142-9152`）；具体实例注入链尚未逐个确认，不推断这些调用都使用上述同一类。

## 2. 真实获取与原始证据

取数时间：2026-10-20 CST（manifest UTC 时间）；目标范围 `2026-01-01..2026-12-31`。深交所按月请求 12 次，间隔约 0.25s，HTTP 200，单次 0.582–0.843s；另 BaoStock 登录后全年查询 1 次，查询耗时 1.319s、365 行。合计 13 次请求/查询。没有失败或重试。深交所请求使用浏览器 UA 与深交所 Referer。

完整请求时间、状态、响应字节数、交易日行数及 SHA-256 在 [`samples/manifest.json`](samples/manifest.json)。深交所逐月原始 HTTP body 在 `samples/szse-2026-MM.json`。BaoStock 原始返回字段 `calendar_date,is_trading_day` 逐行保存在 [`samples/baostock-2026-01-01_2026-12-31.json`](samples/baostock-2026-01-01_2026-12-31.json)；这是 SDK 表格返回行的无损 JSON 封存，不是底层 TCP 协议包。该次查询状态成功，365 行，242 行标记为交易日。

深交所字段：`jyrq`=`YYYY-MM-DD` 字符串，`jybz`=`"1"`/`"0"` 字符串标记，`zrxh` 为整数星期序号；每月返回当月全部自然日。BaoStock 字段：`calendar_date`=`YYYY-MM-DD` 字符串，`is_trading_day`=`"1"`/`"0"` 字符串；全年返回 365 天。两者都表达日期而非带时区时间戳，无时区字段；抓取时间另以 UTC ISO-8601 记录。

## 3. 差异与 Core 现状

- 深交所 vs BaoStock：242 vs 242 交易日；`SZSE only=[]`、`BaoStock only=[]`，逐日一致。
- `holidays.CN()` + 周一至周五估算：242 天；相对两源两侧差异均为空。本结论仅描述本次查询与所用 holidays 包版本，不证明此估算适用于其他年份/交易所；工作日调休与临时休市须以交易所日历为准。
- Core `~/core-data/trade_dates.json`：JSON list，共 76 项，首尾 `2026-06-26` / `2026-10-19`。在文件实际覆盖区间内，与官方两源的 76 天逐日一致；不存在已覆盖日期冲突。与全年的差距是覆盖范围差异，不是缺失交易日的证据。该文件为手写、未发现更新记录，不能据此推断未来覆盖。
- 本次未观察到临时休市或调休造成的两源差异；不能将“无差异”推广为长期一致性结论。

## 4. 规范化建议、降级链、更新

建议一个不可变、版本化的快照：`{schema_version, calendar_id, timezone, declared_valid_from, declared_valid_to, source_guard_valid_from, source_guard_valid_to, trading_dates, source_id, fetched_at, available_at, raw_sha256, semantic_sha256}`。`trading_dates` 为严格升序、唯一的 `YYYY-MM-DD` 日期字符串；交易日集合只包含 `is_trading_day=true`，不把日期转成本地时间戳。`fetched_at` 是 UTC 抓取时刻；`available_at` 表示来源实际可查询时刻，未知则 null，不得伪造。保留原始源行并把来源证据哈希与日期集合语义哈希分开。

建议降级：BaoStock 查询 → 深交所按月 API → 已验证且覆盖请求区间的上一版快照。节假日估算只可显示为 `ESTIMATED/UNVERIFIED`，不得成为正式交易日 authority。每次采集校验日期连续覆盖、格式、唯一性、范围与交易日数量；至少两个来源同窗比对。任一来源冲突则拒绝发布候选版并告警，人工确认后再产生新版本，不任选其一。不存在满足覆盖窗口的可信源/旧快照时 fail closed。未来覆盖只承诺来源明确返回的日期；短窗口滚动预取（建议每晚刷新未来 12 个月并回补最近 30 天），源不提供时保留旧快照并报告边界，不合成日期。

建议原始响应与规范快照按日期/版本只追加保存于受控 Core 数据目录，manifest 记录来源、抓取时间、可用时间、覆盖边界、行数、哈希、差异与发布状态；晚间采集、次日开市前复核最新交易日和未来覆盖。消费者读取同一个版本化快照，禁止各自解析源方言。实际调度、发布位置、保留期和主源需用户定案。

**Core 读取影响清单（只列，不改）**：`src/engine_core/calendar.py` 的 `TradingCalendarSnapshotV1` 是离线快照契约，运行时不联网；`guardian/scheduler.py` 接收日期集合但其上游装载未证实；`data_pipeline/ops.py:81-90` 读 `trade_dates.json` 并期望 list；`prep_trade_day.py:21-25` 兼容 list 或 `{trading_dates: [...]}`；`auction_baseline.py:220-225` 同样兼容两种形状；日报告/report 当前多从调用参数或数据集日期集获得日期，需在切换设计阶段逐个确认依赖。不能直接把丰富对象写入现有 list 消费者路径。

## 5. 证据路径与边界

- 源逐行证据/哈希/获取时刻：[`samples/`](samples/)，特别是 [`manifest.json`](samples/manifest.json)。深交所 12 份响应均保存；BaoStock 结果按 SDK 字段无损保存。
- Core 契约：`/home/exedev/repos/engine_core/src/engine_core/calendar.py`；Core 当前文件：`/home/exedev/core-data/trade_dates.json`（只读检查）。旧系统源码位置见 §1。
- 查询统计和差异为本次对保存响应的逐日集合比较；不涉及 Redis/TD 写入，不改 Core 数据或旧系统。
