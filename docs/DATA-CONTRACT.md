# 数据契约 v1

本规格约束 catalog 数据版本、报告读取和登记表声明。`UNKNOWN` 是显式未知值，不能解释为零、完整或否。

## 1. 四个时刻与历史导入

每个版本的 manifest 分别记录以下字段，格式为带时区的 RFC 3339 时间；日期字段用 `YYYY-MM-DD`：

- `business_date`：数据所描述的业务日，不表示采集或可见时刻。
- `source_asof`：源端内容对应的截止/快照时刻；源不提供时写 `UNKNOWN`，不得用抓取时间代填。
- `captured_at`：本次采集或计算实际完成的时刻；由执行端实测记录，不能按计划时间推定。
- `available_at`：该版本实际成功写入 catalog 的时刻；必须在提交时实测，禁止回填、推定或从业务日计算。
- 历史导入另写 `authority: "IMPORTED"`；其 `available_at` 是该版本首次进入本 catalog 的时刻，不是历史内容原本可见的时刻。未知首次入库时刻必须保持 `UNKNOWN`，不得伪造 PIT 证据。

正例：C415 的 plate_members 10-20 版实际在 10-21 08:37 抓取，却登记为 10-20 00:00 可用；正确记录应把 `business_date=2026-10-20`、`captured_at=2026-10-21T08:37:...+08:00`，并以实际 catalog 提交时间填 `available_at`。

反例：把 plate_members 的 `available_at` 倒填为 10-20 00:00，或把 daily_kline 的业务日 D 当作其可用时刻（C414 daily_kline）。

## 2. 版本选择与报告输入

对报告 `asof`，每个数据集只可选择满足 `available_at <= asof` 的版本；不得按分区日期、目录序号、文件修改时间或“最新版本”替代该条件。缺失 `available_at` 或不可解析时，该版本不能证明在截止时刻可见。报告 manifest 必须逐项记录输入的 dataset、version、`available_at`；缺失项显式记录为 `UNKNOWN`，不得静默省略。

正例：C415 plate_members 的 10-21 08:37 实际抓取版本，对 10-21 08:00 的报告不可选。

反例：沿用该档案中声明的 10-20 00:00 `available_at`，让 10-21 08:00 报告选中尚未采集的内容；或只记录目录 `auction_0926-001` 而不记录输入版本（C416 auction_report）。

## 3. 唯一 producer 与重建命名

每个活跃 `catalog_dataset` 只能有一个 registry `producer`，格式 `相对仓库文件:函数`。所有其他入口只读；确需研究或重建写入时必须写入独立的 `<dataset>_rebuild` 数据集，且生产切片不得选取它。多个入口向同名 dataset 写入，即使数据相似，也违反本条。producer 尚未查明时登记 `UNKNOWN` 并保持 OPEN，不能把 owner 名称冒充代码位置。

正例：C416 证明 `name_map` 没有独立写入，`stock_name_map` 是 `stock_static` 的读取投影；因此登记为 stock_static 视图，不另设 producer。

反例：C414 的 auction_points 同时由 TD 重建与 Redis 归档写入同一 dataset；C416 的 auction_panorama 同时有实时、报告内和盘后重建入口。不得将它们都登记成同一生产 dataset 的 producer。

报告的夜间重跑若仍生成 close 产品，登记为 `close_report` 的修订身份，不另称独立 night 产品（C416 night_report）。

## 4. READY 判据、缺失语义与告警

每个活跃 registry item 声明 `ready_criteria`，字段至少包括 `min_rows`、`coverage`、`time_range`；每个值必须是可判定的具体门槛或显式 `UNKNOWN`。READY 当且仅当所声明门槛均可计算且满足，且版本时点满足第 2 条。未知门槛不能默认为通过。guardian 后置条件必须验证 READY 判据；数据缺失或覆盖不足时，报告语义为“未知”，不得转成“否”；同一数据项/分区的缺失告警只发一次。

声明格式示例：`{"min_rows": 1, "coverage": {"min": 0.99, "denominator": "symbol_universe"}, "time_range": {"from": "09:15:00+08:00", "through": "09:26:00+08:00"}}`。证据不足时各键写 `UNKNOWN`。

正例：C414 daily_indicators 10-20 首版为 0 行，应判未 READY；缺少涨停理由时报告为未知，而非“无理由/否”。

反例：C415 kaipan_refresh_0850 仅因 limit_up_pool 检查成功就判整个盘前数据 READY；审计确认这不能证明理由、热板和成员数据可用。

## 5. 报告成品索引

`report_index` 每条记录使用字段：`date`（业务日）、`stage`（产品阶段）、`asof`（内容截止时刻）、`completed_at`（实际完成时刻）、`input_versions`（dataset 到 `{version, available_at}` 的映射）。同一 date/stage 可有多个修订；按索引时刻和输入版本区分，不用目录序号充当身份。未知值显式为 `UNKNOWN`。

正例：close 与夜间重跑均登记为 `stage=close` 的不同修订，并各自记录 `asof`、真实 `completed_at` 和输入版本集（C416 close_report/night_report）。

反例：C416 发现报告只落在 `reports/<date>/<checkpoint>`，无可查询 `available_at` 或输入版本；目录名不能回答“截至 T 看到了什么”。

## 6 登记表源单位码

源单位码格式为 `<unit>:<encoding>`；单位为 `yuan/milli_yuan/percent/bp/shares/hands/epoch_ms/yyyymmdd/iso_date/local_datetime_cst/none/category/count`，编码为 `float/int/numeric_string/str/bool`。`UNKNOWN` 表示单位未核实，`ABSENT` 表示来源没有该字段。
换算使用 Decimal 与 `ROUND_HALF_UP`（远离零）；元价转毫元乘 1000，百分数转 bp 乘 100，元金额及股数取整，手转股乘 100。
`None`、空字符串和 `--` 规范为 null，不当作零；日期时间分别规范为 ISO 日期与带 `+08:00` 的 ISO 时间。
来源适配器按 manifest.adapter、最后一条 ACCEPTED provider_attempt、精确 source 标签的顺序选择；目录中的组合来源标签不是适配器。

登记表的 `source_keys` 可用 `@ctx:<name>` 从调用上下文取值；上下文缺键按必填/可空规则标记。
`raw_only=True` 保留原值，不换单位或检查类型，只应用 nullable 约束。
检查状态分为 `OK`、`BELOW_MIN`、`INERT`（零次求值）与 `INAPPLICABLE`（来源声明缺少所需字段或检查不适用于该来源）。
相对价格关系校验需要盘面阶段提供前一交易日收盘价作为上下文。
