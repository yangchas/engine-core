# 指标框架横切规范

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。状态：设计合同；标为“拟新增”的字段、表、命令和行为不是现行实现。未决项阻止相应细则和实现宣称完成。来源缩写：C784 = `/home/exedev/fw/reports/C784.md` 及 `/home/exedev/validation/realdata/indicator_samples_20261107/REPORT.md`；C782/C783/C774 = `/home/exedev/fw/reports/` 对应报告；代码路径相对仓库根。示例若明确标“合同示例”，只说明格式，不宣称是采集数据。时间均为 Asia/Shanghai，as-of 使用带时区时间。

## §0 目的与总原则

- 已收盘交易日每项事实只有一个获取方；首次成功发布后消费者复用，修订由获取方发布新版本。指标函数不得直接读取 market_data1 大表或外部接口。需要显式拉取时遵守 §4 租约。
- 按依赖和失效边界分簇分表；同簇同一输入版本一次遍历，共用中间量只计算一次。输入及算法版本未变时不再计算；不同函数共用 L0，不能各自获取。
- 日K L0 不复权（Baostock `adjustflag=3`，`src/engine_core/daily_history.py:536`）；复权事件独立存储；价量和筹码簇使用前复权价，算法版本记录复权口径。新除权事件使该股全部历史递推指标失效，从头重算。
- 金额保持来源原类型：日K amount_yuan DOUBLE，竞价 match_amt_yuan 和 tick amt_yuan BIGINT（C784 DESCRIBE）；指标及比率 DOUBLE，缺失 NULL，不补零。
- TD `core_data` 为冷层和唯一真相源，保留 3650 日；Redis `core:ind:` 缓存最近 5 个交易日，TTL 604800 秒（DECISION-5）；丢失从 TD 重建，不触发上游。market_data1 数据库 KEEP 60 日，64 日基线的原始事实必须先存 core_data（C784）。
- 预热不足必须 `quality='PARTIAL'`、`warm=false`；未达完整性门槛的依赖默认不计算。每个函数可独立运行并自行检查依赖。

## §1 术语与分层

L0 是有来源、时点和版本的市场事实，不是策略结论；L1 是依赖已发布事实计算出的指标和状态；L2 是报告/筛选消费，不反写事实。簇是输入、触发和失效边界兼容的一组 L1；获取方是某项 L0 的唯一发布者。版本标识输入内容及来源，`available_at` 是消费者可见时刻，不能用未来可见的版本回答历史 as-of。交易日历决定窗口，不用自然日代替。

## §2 L0 事实清单

以下每节的 1–11 顺序固定：名称、含义、获取方/接口、时点频率、增量/水位、TD/DDL、日标记、Redis、真实样例、代码、差距。字段来源以小节括号内来源统一标注，拟新增字段另注明。日标记设计复用 `daily_kline_versions` 的 `trade_date/version/business_status/row_count/sha256/available_at`（`td_sink.py:302`）；日K按 active_symbols 覆盖率≥0.995；0925 和 0945 分别有效标的≥4000（plate_amount_baseline.py 的 MIN_FINAL_SYMBOLS）；其余阈值 UNKNOWN-U1。停牌、退市、未上市、源空使用带原因白名单，不记零（DECISION-4）。日标记在数据写完、核对行数与摘要后最后发布；缺行或未知阈值不得标完整。所有拟新增表须单独经过 DDL 审批；下列 DDL 是设计文本，不是已执行事实。`ts` 为交易日上海时区零点转换的时间戳；事件事实一般以事件日为 ts；adjust_factor 快照以处理时刻为 ts。新增事实热视图键与 TTL UNKNOWN-U4，须逐事实设计；现行 daily_dde 键保持不变（DECISION-5）。数据修订不得覆盖已有可见版本：物理隔离方式见 UNKNOWN-U2。

### 2.1 daily_kline（来源 C784；`src/engine_core/daily_history.py:23,536`；`src/engine_core/data_pipeline/td_sink.py:147,295`）

1. 名称：`daily_kline`。2. 不复权日 OHLC、成交与状态。3. 现行日K获取链使用 Baostock `query_history_k_data_plus`；请求字段 `date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,tradestatus`，`adjustflag=3`；唯一发布方及多来源切换边界待 UNKNOWN-U3。4. 收盘后按交易日增量。5. 按股、交易日与已发布版本检查，缺口按交易日补齐，不以最大日期证明全市场完整。6. 实测表（金额现为 DOUBLE）：
```sql
CREATE STABLE IF NOT EXISTS core_data.daily_kline_v1 (ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE, pct_chg DOUBLE, volume_shares BIGINT, amount_yuan DOUBLE, turnover_pct DOUBLE, suspended BOOL, version VARCHAR(24)) TAGS (symbol VARCHAR(8));
```
7. 现行 `core_data.daily_kline_versions` 列与类型见 C784；完整阈值见 DECISION-4。8. 热视图键与 TTL UNKNOWN-U4，现行视图精确键 UNKNOWN-U4。9. 真实样本：2026-11-06 `symbol=600519, close=1453, volume_shares=4284772, amount_yuan=6199528289.3, turnover_pct=0.3428`（C784）。10. 代码见标题。11. 保留 DOUBLE 小数，不转换（DECISION-6）。

### 2.2 adjust_factor（来源 C784）

1. `adjust_factor`。2. 除权事件因子快照。3. Baostock `query_adjust_factor`，响应 `code,dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFactor` 均字符串（C786）；获取方新增。4. 收盘后用 pre_close 与前一交易日 close 不等发现迹象并仅对疑似股拉取，阈值和误报 UNKNOWN。5. 每股单行 JSON 快照，单次 INSERT 原子替换；U2 对本事实不再成立。6. DDL 见 §2.9，因子原字符串保留并在读取时以 Decimal 解析。7. 日标记见 §2.9，完整性阈值 UNKNOWN-U1。8. 事实热视图键及 TTL UNKNOWN-U4。9. 600519 的 2025-06-26 foreAdjustFactor=0.960527，前一日所用旧因子 0.942063（C786）；完整事件行其他值 UNKNOWN-U5。10. 获取方新增。11. 最早事件前不补 1，标 PARTIAL；新事件前后整股快照重发待实证（DECISION-3）。

### 2.3 daily_dde（来源 C784；`src/engine_core/data_pipeline/daily_dde.py:78,196,424`；`src/engine_core/guardian/datasets.registry.json:212`）

1. `daily_dde`。2. 每股每日 DDJE 原值。3. 现行 `run_daily_dde_sync` 调用开盘啦 `getHisStockDDE`，响应 `Date` 为 YYYYMMDD 字符串数组、`DDJE` 整数数组、`ttag` 浮点、`errcode` 字符串；`ttag` 含义未知，DDJE 单位按元保存，中等置信度，官方定义 UNKNOWN-U6（C788）。4. 盘后；registry 15:12 与 timeline 21:00 冲突，见 U7。5. 每股水位补缺，拒绝源最新日早于目标日，不能把空值当零。6. 实测：
```sql
CREATE STABLE IF NOT EXISTS core_data.daily_dde_v1 (ts TIMESTAMP, ddje DOUBLE, version VARCHAR(24)) TAGS (symbol VARCHAR(8));
```
7. 拟新增共同日标记，门槛 UNKNOWN-U1。8. 现行 `core:cache:data:daily_dde:{date}` hash，symbol→JSON，TTL 86400 秒；不是新指标热键。9. 2026-11-06 三股各返回最近 30 日，与 TD 90/90 相等（C784）；真实配对：300750，2026-10-21，ddje=-1340525000、amount_yuan=32439007670.33（C788）。10. 见标题。11. 无 `large_net/ddx/ddy/ddz` 来源；不能编造；仅派生 ddje_ratio=ddje/amount_yuan，分母为零或 NULL 时为 NULL；单位置信度见 C788。

### 2.4 auction_amount（来源 C784；C782；分支 `C778-auction-amount-facts-store` 的 `src/engine_core/data_pipeline/auction_amount_facts.py`）

1. `auction_amount`。2. 09:25 竞价匹配金额。3. 唯一获取方拟从 `market_data1.auction_snapshot_v2` 提取，原字段 `ts,match_amt_yuan,symbol,trade_date,auction_tag`（C784）；C778 未合并，其接口不能当作已上线。4. 09:25 后可用时逐交易日抓取一次。5. 每日校验目标股及固定时点，保存实际源时间，补历史仅由获取方进行。6. 拟新增冷表：
```sql
CREATE STABLE core_data.auction_amount_v1 (ts TIMESTAMP, match_amt_yuan BIGINT, version VARCHAR(24)) TAGS (symbol VARCHAR(20));
```
`ts` 为交易日零点；精确选点及源时间字段 UNKNOWN-U8，DDL 尚不能投入生产。7. 共同日标记，门槛分别为≥4000（DECISION-4）。8. 热视图键和 TTL UNKNOWN-U4。9. 真实单行值 UNKNOWN-U8（需 runner 从 C784 原始响应核对）；不得以构造值代替。10. C778 分支；现行 `src/engine_core/auction_baseline.py:188` 仍读 TD。11. 60 日源保留不足 64 日，冷存必须先建并回填。

### 2.5 slot0945_amount（来源 C784；`src/engine_core/plate_amount_baseline.py:130,193`）

1. `slot0945_amount`。2. 09:45 时点累计成交金额。3. 拟新增唯一获取方读取 `market_data1.stock_tick_v2` 的 `ts,amt_yuan,symbol`；`amt_yuan BIGINT`（C784）。4. 09:45 后逐交易日。5. 时间边界与选点规则 UNKNOWN-U8，不得把竞价虚拟匹配额加到累计成交额上。6. 拟新增：
```sql
CREATE STABLE core_data.slot0945_amount_v1 (ts TIMESTAMP, amt_yuan BIGINT, version VARCHAR(24)) TAGS (symbol VARCHAR(20));
```
源时间保留和选点依赖 U8。7. 共同日标记，门槛≥4000（DECISION-4）。8. 热视图键和 TTL UNKNOWN-U4。9. 真实单行值 UNKNOWN-U8。10. 见标题。11. 现行基线直接读 tick，需先迁到唯一 L0 获取方。

### 2.6 plate_members（来源 `src/engine_core/plate_amount_baseline.py:49,292`；C782）

1. `plate_members`。2. 某交易日板块与股票关系。3. 已有消费者接受 `plate_code` 和 `symbol`；唯一获取方及实际源 UNKNOWN-U9。4. 每交易日形成可追溯快照；历史查询禁止使用当前关系冒充历史。5. 按日版本与成员集合摘要判定变化。6. 拟新增 DDL 物理主键和 tag 设计 UNKNOWN-U2/U9：`CREATE STABLE core_data.plate_members_v1 (ts TIMESTAMP, ... UNKNOWN-U9 ...) TAGS (... UNKNOWN-U2 ...);`。7. 共同日标记，覆盖与阈值 U1。8. 热视图键和 TTL UNKNOWN-U4，实体必须包含板块和股票，编码 U9。9. 真实一行 UNKNOWN-U9。10. 见标题。11. 历史快照覆盖与来源需 runner 核查。

### 2.7 symbol_universe（来源 `src/engine_core/data_pipeline/derived_daily.py:189`；C782）

1. `symbol_universe`。2. 每日应覆盖股票集合。3. 已有血缘引用 `dataset,trade_date,version,business_status`；唯一获取方与源字段 UNKNOWN-U10。4. 按交易日快照。5. 以日期、版本与成员摘要校验，不能以前一日最大水位替代。6. 拟新增 DDL：`CREATE STABLE core_data.symbol_universe_v1 (ts TIMESTAMP, ... UNKNOWN-U10 ...) TAGS (... UNKNOWN-U2 ...);`，不得执行占位文本。7. 共同日标记，上市、退市、停牌规则 U1。8. 热视图键和 TTL UNKNOWN-U4。9. 真实一行 UNKNOWN-U10。10. 见标题。11. 与各源可查询股票集合的差异必须留缺口证据。

### 2.8 limit_up_pool/detail（来源 C782；`src/engine_core/guardian/datasets.registry.json:437,2156`）

1. `limit_up_pool` 与 `limit_up_detail` 两个事实。2. 每日涨停池及明细。3. 池现有来源开盘啦→问财→TD 日K，明细依赖池并取开盘啦；各唯一发布方及源字段 UNKNOWN-U11。4. 池取 D-1，分别 00:40、00:50 后触发（C782）。5. 逐日检查版本、池明细关系；明细现缺日默认回补 20 日。6. 现有池表 `core_data.limit_up_pool_v1` 与 `limit_up_pool_versions`（C783）；列和 detail 表 DDL UNKNOWN-U11：`CREATE STABLE core_data.limit_up_pool_v1 (ts TIMESTAMP, ... UNKNOWN-U11 ...) TAGS (... UNKNOWN-U11 ...);`；detail 同理，不得执行占位文本。7. 各自标记与覆盖阈值 U1，不以池非空证明明细完整。8. 各自已有 Redis 视图的键与 TTL UNKNOWN-U11；新视图键及 TTL UNKNOWN-U4。9. 真实一行 UNKNOWN-U11。10. 见标题。11. 统一血缘、降级源语义及完整性待核对。

## §3 L1 通则

一簇一表：逻辑键 `(cluster, entity, trade_date, version)`，每个计算结果与递推状态必须原子可见。TD 的同子表同 ts 覆写会丢版本；实际子表/version 隔离方案 U2 冻结前不得实现。固定元列 `version,algo_version,input_refs,available_at,quality,warm`（拟新增；版本与血缘参考 `derived_daily.py:189,208`）；状态列随簇定义，不能只保存在进程内。`input_refs` 按 dataset、trade_date 排序的 JSON，例（格式示例，字段见 `derived_daily.py:189`）：
```json
[{"dataset":"daily_kline","trade_date":"2026-11-06","version":"example","business_status":"FORMAL"}]
```
SHA-256 对规范 UTF-8 JSON、算法版本及参数计算；版本缺失或不可验证时不跳过。金额保留源类型；价量指标 DOUBLE，未知 NULL。预热不足保留可计算值但 PARTIAL/false；缺关键输入不发布结果。通用 DDL 模板（`<...>` 为设计占位，非可执行 SQL；列宽与 tag 映射需 U2）：
```sql
CREATE STABLE core_data.<cluster>_v1 (ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), input_refs NCHAR(16384), available_at VARCHAR(40), quality VARCHAR(16), warm BOOL, <cluster_columns>) TAGS (symbol VARCHAR(8), <version_isolation>);
```
价量簇前几列的具体合同示例（拟新增）：`ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), input_refs NCHAR(16384), available_at VARCHAR(40), quality VARCHAR(16), warm BOOL, ema_12 DOUBLE, ema_26 DOUBLE, macd_signal DOUBLE, k DOUBLE, d DOUBLE`；计算参数及初始化未冻结，见 U12。输入引用超过列宽须拒绝发布并改存可校验引用，不得截断。

## §4 依赖协议

在 `datasets.registry.json` 每个 dataset 对象新增顶层 `requires` 与 `produces`（拟新增，当前未实现；现行 registry 见 `:276`）。格式示例：
```json
{"requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"},{"dataset":"adjust_factor","window_days":120,"min_quality":"FORMAL"}],"produces":["price_volume_indicators"]}
```
`window_days` 为交易日，实际回填长度由细则确定，120 不是稳定性保证。每个依赖检查全部日期、实体、完整标记、版本和 as-of；每个缺项必须携带 dataset 与日期。伪代码：
```text
dates = calendar.resolve(date, requires)
refs, missing = inspect_all(requires, dates, available_at)
if missing and pull_upstream:
    for fact, day in missing.fact_dates():
        with renewable_lease(fact, day): invoke_unique_acquirer(fact, day)
    refs, missing = inspect_all(requires, dates, available_at)
if missing: return 30, NOT_YET_AVAILABLE(missing)
if not force and committed_same_hash(refs, algo_version): return 20
result, state = calculate_once(refs)
validate_and_commit_then_publish(result, state)
return 0
```
拟新增租约键 `core:lease:fact:{fact}:{date}`，TTL 300 秒，持有者随机 token；续租和释放需比较 token，丢租即停止发布。租约只协调获取方，失败后重新读完整标记，不许并发重复获取。现有 `chain.py:148,171` 顺序及运行后重读可复用；`derived_daily.py:189,293` 血缘及失效判断需扩展日期/实体和缺版本处理；`guardian/chain_sweep.py:44,64` 现行租约 `core:lease:chain_sweep:{config.name}` 默认 300 秒，可复用机制而非键。回复合同见 §8。

## §5 聚类

判定树：先比较事实集合及实体；不同则分簇。再比较触发点与失效传播；不同则分簇。再比较窗口及质量要求；不能安全共享则分簇。最后检查是否能共用一次遍历及中间量；不能则独立。新指标先登记精确依赖、计算公式、最小预热、状态、触发、失效、测试和消费方，再按树归簇；不得以名称相近强合。

| 簇（拟新增） | 成员 | 共用输入 | 中间量 | 触发 | 原因 |
|---|---|---|---|---|---|
| price_volume | MA、EMA、MACD、KDJ、BOLL，RSI/ATR 待细则 | daily_kline、adjust_factor | 前复权价、滚动高低/均值、EMA | 日K与因子完成后 | 同股同日价格遍历 |
| chip | 筹码峰 | 日K、因子、换手及 DECISION-1 输入 | 分箱分布 | 同上 | 状态和模型失效独立 |
| dde | DDJE 原值及已证实派生 | daily_dde；比率另需日K金额 | 分母 | DDE 完成后 | 来源独立；无来源字段禁止输出 |
| auction_baseline | 竞价窗口统计 | auction_amount | 滚动窗口 | 盘前 | 时点与实体一致 |
| plate_amount_baseline | 板块 09:25/09:45 金额统计 | 两种金额事实、逐日 plate_members | 每日板块总额 | 盘前 | 同一成员及窗口；时点仍分别记录 |
| ladder | 涨停梯队 | pool/detail、日K | 连板状态 | 输入完成后 | 独立事件及回溯 |

## §6 增量与失效

按日期升序，先解析同一份前复权输入，再每股每根 K 线一次更新滚动高低点、均值/方差、EMA、K/D 及各指标；各中间量在同簇仅更新一次。递推状态拟用同版本同日 DOUBLE 列 `state_ema12,state_ema26,state_dea,state_k,state_d`（§3.1 拟新增），公式、初始值和精度 U12；必须从前一有效检查点恢复，检查点血缘不匹配则回退。窗口型读取或保存完整窗口；中位数/分位数不能只保存和与计数。筹码是窗口型：每个交易日用最近至多 120 根前复权 close 重新分箱计算，只存输出列，不存分布向量（DECISION-1）。一次性事实按实体日检查已发布完整版本并复用；修订另发版本。

| 事件 | 影响范围 | 动作 |
|---|---|---|
| 某股新除权事件 | 该股全部历史递推价量和筹码 | 全历史失效，从头按新因子重算 |
| D 日普通价格修订 | 该股递推输出 D 起至最新 | 从 D 前有效状态重算 |
| D 日窗口事实修订 | 所有包含 D 的对应窗口和实体 | 仅重算受影响窗口 |
| 成员快照修订 | 使用该日成员的板块窗口 | 重算受影响板块及窗口 |
| DDE 修订 | 对应股日及其依赖窗口 | 重算 DDE 簇相关结果 |
| 公式、参数或复权口径变更 | 使用该算法版本的全部结果 | 新算法版本全量回填 |
| 热键丢失 | 缓存 | 仅从 TD 重建 |

首次回填按交易日从最早可用历史顺序计算；每指标细则冻结所需历史和误差容忍度。C591 的 120 根是筹码输入候选而非通用稳定保证；具体最小预热 UNKNOWN-U12。仅当全部输入引用、算法版本、参数哈希相同且结果已完整提交时跳过；缺血缘视为未知，不能跳过。

## §7 热冷存储

| 类别 | 冷层 | 热层 |
|---|---|---|
| L0 | core_data 事实及完整标记 | 事实视图，不能替代冷层 |
| L1 | core_data 簇结果、状态与版本 | 最近一日及最近 N 日 |
| L2 | 消费可见版本，不作为事实 | 按消费需要，不反向写 L0 |

拟新增指标 hash：`core:ind:{cluster}:{YYYY-MM-DD}`，field 为实体，value 为 JSON；TTL 604800 秒。例如合同格式（拟新增字段）：`{"version":"example","algo_version":"example","available_at":"2026-11-06T21:00:00+08:00","quality":"PARTIAL","warm":false,"close":1453.0}`，其中 close 的真实值来自 C784，其余是格式示例。N=5 个交易日（DECISION-5）。TD 现有表 `daily_kline_v1,daily_kline_versions,daily_dde_v1`；拟新增 §2 事实表、每簇表和各自完整标记，物理版本隔离 U2。Redis 清空后按 as-of 从 TD 读取最近合格版本、核对标记并重建 hash/TTL；TD 无合格数据返回缺依赖，绝不回源。生产路径禁止中间文件作为事实交换或权威存储（fw/design/31）；已有 catalog 迁移期间仅作为兼容读取，退出条件见 §10。

## §8 函数契约

拟新增入口 `run(date, window, pull_upstream=False, force=False, dry_run=False, available_at=None, td_split_retry_depth=None) -> (exit_code, reply)`；CLI 为 `--date --window --pull-upstream --force --dry-run --available-at`，只有适用 TD 拆分读取的获取方接受 `--td-split-retry-depth`（现有 `plate_amount_baseline.py:161`）。`--force` 重算但不能绕过依赖及租约；dry-run 不写入、不拉源。相同输入引用、算法与参数及完整提交是幂等键。输出遵循 `src/engine_core/guardian/contract.py:14`，所有代码须同时登记到 `error_codes.json` 与运行时矩阵。

| exit | 状态 | error_code | 登记情况/语义 |
|---|---|---|---|
| 0 | ok | 空字符串 | 已登记（成功状态，无 error_code）；`contract.py:32` |
| 10 | degraded | `STALE_VERSION` | 新增；使用合格旧版本，非重试 |
| 20 | not_applicable | `ALREADY_SATISFIED` | 已登记；幂等跳过 |
| 30 | failed | `NOT_YET_AVAILABLE` | 新增；依赖未就绪，可重试 |
| 40 | failed | `CONTRACT_ERROR` | 已登记；确定性合同错误，不重试 |
| 64 | failed | `CONFIG_ERROR` | 已登记；配置错误，不重试 |
| 75 | failed | `BAOSTOCK_CONNECTION_ERROR` | 已登记；例示上游或执行暂时故障，可重试 |

`NOT_YET_AVAILABLE`、`STALE_VERSION` 当前未登记（`guardian/error_codes.json`），必须先扩展注册及 native 解码矩阵；不能假设未知对被正确解释。示例（拟新增 `detail` 内容）：
```json
{"contract":1,"status":"ok","error_code":"","retryable":false}
{"contract":1,"status":"degraded","error_code":"STALE_VERSION","retryable":false,"detail":{"quality":"PARTIAL"}}
{"contract":1,"status":"failed","error_code":"NOT_YET_AVAILABLE","retryable":true,"detail":{"missing":[{"dataset":"daily_kline","trade_date":"2026-11-06"}]}}
{"contract":1,"status":"not_applicable","error_code":"ALREADY_SATISFIED","retryable":false}
```

## §9 编排与配置模板

现有 `timeline.json` 有 functions、events、policy 和 `on_deadline`；`plate_amount_baseline_0815` 为 08:15 触发、08:25 截止，`policy_12` 一次、540 秒（C774；`timeline.json:1891,2035`）。以下 `requires/produces` 是新增字段，其他字段需实施卡对照现行 schema 校验：
```json
{"requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"}],"produces":["price_volume_indicators"]}
```
函数配置绑定可独立执行入口；事件配置 `id`、`fn`、交易日触发和 deadline；policy 设置超时、次数与退避。每簇按 L0 完成事件触发；独立运行也执行 §4 检查；有依赖变更时 sweep 重读并安排失效范围。降级只选 `available_at <= as-of` 且不晚于目标交易日的最新合格版本，输出 `quality=PARTIAL` 并记录来源日期及版本；不能把旧值冒充当日完成。现有 `on_deadline: degrade` 不表达最终失败后自动降级（C774）；需新增最终失败动作及降级选择配置、版本/时点限制、输出状态规则，具体 schema 名和 native 支持 UNKNOWN-U13，不能把拟议配置当已实现。

## §10 迁移映射表

| 当前对象（来源 C782） | 目标及步骤 | 风险和旧路径删除条件 |
|---|---|---|
| daily_indicators | 价量簇；先补 L0/因子，再实现并双读验证 | 120 日重算与前复权新算法不同；消费者迁完、对账及回滚窗口通过才删 |
| daily_report_features | L1 消费组合或 L2；保留报告接口直至迁完 | 需逐字段分类及消费核对 UNKNOWN-U14 |
| ladder_daily | ladder 簇，输入 pool/detail 和日K | 现 catalog 权威、TD v1 未维护；同上且历史血缘完整 |
| stock_day_facts | 登记构建器的日K派生归合适簇；独立多阶段 CSV 构建器另审 | 两套互不相干（`data_pipeline/ops.py:21`、`stock_day_facts.py:79`），不得一起删；逐消费者证明迁完 |
| daily_dde | 保留 L0 获取方；派生归 DDE 簇 | registry 15:12 与 timeline 21:00 不一致，U7；官方单位仍 UNKNOWN-U6 |
| auction_baseline | auction_amount L0 后迁窗口簇 | 先冷存足够历史并验证 fallback 语义，消费者迁完再删直读 |
| plate_amount_baseline | 两种金额事实及成员 L0 后迁窗口簇 | 先持久化 64 日，保留历史成员版本与 as-of 降级；消费者迁完再删直读 |
| C778 auction_amount_facts | 审核未合并分支作为 L0 候选 | 接口及存储必须与本规范核对；合并与切换前不得当现行代码 |

该分支 `auction_amount_facts.py:13` 提供 `ensure_schema/complete_days/read_days/write_day`；其 `auction_amount_facts_v1` 为 `ts,amount_yuan BIGINT`，tag 为 `symbol,slot`，日表为 `auction_amount_facts_days`。`write_day` 先删除完成标记和旧行再写入；它没有本规范要求的不可覆盖版本隔离和输入血缘，不能直接视作满足目标。其 0925/0945 共表方案还须按事实独立完成与失效边界评审。

迁移顺序：冻结决策和事实细则 → 唯一获取/冷存/标记 → 真实回填和依赖协议 → 簇增量与双读对账 → 切片及编排切换 → 经消费清单、真实窗口验证和回滚期后删除旧路径。旧算法迁移先 fixture/evidence，禁止导入或复制旧项目源码。

## §11 验证策略

每张实现卡先写因旧行为失败的最小测试；解析与计算只回放 `examples/capture_real_fixture.py` 抓取并登记 `tests/fixtures/real/PROVENANCE.json` 的真实原始响应，不能手写假行。C784 的 `baostock_raw.jsonl`、`kaipan_raw.jsonl`、`td_raw.jsonl` 是研究证据，需 runner 按项目捕获流程重新采集/登记后才可当测试夹具；不要直接冒充已登记夹具。新数据源加 `pytest --live` 契约测试。因子由 runner 抓跨除权事件的原始记录和供应方口径；金额、成员、股票池、涨停明细由 runner 抓带时间与来源的真实响应（U8–U11）。

每类函数至少验证：L0 同日第二次不调用上游、缺行不发布完整；L1 缺一个日期返回 30 且 detail 指明事实日期；递推接续与同一真实短窗口从头计算一致；窗口含修订日的结果重算，其他窗口保持版本；新除权事件使该股全部历史递推重算，其他股票不变；warm 不足为 PARTIAL/false；金额类型保持按 DECISION-6。筹码先用 `/home/exedev/validation/realdata/chip_dde_20261107/inputs.jsonl` 与 `expected.jsonl` 复现不复权输入 10 股 100/100 项（C788，结合 PROVENANCE.json），按项目捕获规则登记后用于测试；前复权输入另由实现卡使用同一算法生成并标注独立期望，不得把旧不复权输出直接当作前复权期望。使用真实 TD 结果重建被隔离的测试 Redis 热键，核对版本、值和 TTL，并断言无上游调用；不能删除非 `core:` 键。as-of 降级不能读到未来版本。验证最短可触发窗口，记录原始命令与输出及 fixture 来源，不用全量回放冒充覆盖。

## §12 决策点与 UNKNOWN 清单

| 编号 | 选项、倾向、影响 |
|---|---|
| DECISION-1 | **已决定**：方案 A，兼容 C787 runtime_chip_runner；输入前复权 close 和百分数 turn，最近至多 120 根、至少 5 根；窗口最低至最高价动态等分 50 箱。逐日 t=min(turn/100,1)，各箱乘 1-t，再给收盘价所在箱加 t。缺 turn 的当日输出 NULL、该股窗口 PARTIAL，不补 0；不持久化分布，逐日重算。峰价为最大权重箱中心，峰权重为占总权重百分数，平均成本为箱中心加权均值；获利为中心价≤末日收盘价的权重占比，亏损=max(0,1-获利)；70%成本区间为累计 15%/85% 箱中心，集中度为权重降序覆盖70%所需箱数/50，密集箱数为权重严格大于总权重5%的箱数（C787/C788）。输出 peak_price、peak_weight_pct、avg_cost、profit_ratio、loss_ratio、cost_low_70、cost_high_70、concentration_70、dense_bins、chip_percent；旧名映射分别为 peak_weight、lower_cost、upper_cost、concentration、dense_area_count，chip_percent 原名保留（C788 expected.jsonl）。全同价、总权重非正及负换手边界依 C787，精确退化字段 UNKNOWN（需筹码细则卡）。理由：C788 不复权样本 10 股 100/100 项一致；影响：前复权输入结果与旧不复权样本不可直接比较。 |
| DECISION-2 | **已决定**：存 DDJE 原值，按元标注，中等置信度；C788 1200 对绝对值比成交额中位数 0.0491233、P99 0.202047、最大 0.401812、无大于1，推断依赖净额子集假设。仅派生 ddje_ratio=ddje/amount_yuan，分母零或 NULL 输出 NULL；large_net/ddx/ddy/ddz 无来源、不复现、不补0；ttag 语义 UNKNOWN，不存。影响：比率单位仍待权威核实。 |
| DECISION-3 | **已决定**：前复权 OHLC=原价×当日及以前最近事件 foreAdjustFactor，事件日用新行，前一交易日用上一行；后复权用 backAdjustFactor（C786 五股逐日比较）。最早事件前 UNKNOWN，该股窗口 PARTIAL。因子按整股快照版本存，发现新除权整股替换；新事件导致旧因子重发尚 UNKNOWN，实施卡第一步用真实新除权样本验证，在验证前仍整股替换。用日K pre_close 与前一交易日 close 不等作为迹象，仅对有迹象股票重拉，阈值与误报待真实数据验证。影响：重算该股历史，旧快照仍须满足 U2 版本隔离。 |
| DECISION-4 | **已决定**：daily_kline 按 active_symbols 覆盖≥0.995；竞价 09:25 与 09:45 各有效标的≥4000（plate_amount_baseline.py MIN_FINAL_SYMBOLS）；其他事实阈值 UNKNOWN（需数据负责人查）。停牌、退市、未上市、源空使用日标记中的带原因白名单 SUSPENDED、DELISTED、NOT_LISTED_YET、SOURCE_EMPTY；缺失永不记0。影响：未知阈值不发布完整标记。 |
| DECISION-5 | **已决定**：指标热层最近5个交易日，core:ind:{cluster}:{YYYY-MM-DD}，hash field=symbol，value=JSON，TTL 7天；事实沿用现有命名，daily_dde 为 core:cache:data:daily_dde:{date}、TTL 86400（daily_dde.py），新事实键待迁移定义，不能把旧键当新键。影响：缓存丢失从 TD 重建。 |
| DECISION-6 | **已决定**：金额按源类型原值存，不转换；daily_kline amount_yuan DOUBLE（真实值 6199528289.30），竞价 match_amt_yuan 与 tick amt_yuan 均 BIGINT（C784 DESCRIBE）；指标、比率 DOUBLE。理由：保留上游精度；影响：C778 接口整数金额仅用于上述 BIGINT 来源。 |

| 编号 | 待查及责任 |
|---|---|
| UNKNOWN-U1 | 数据负责人核对除日K和两个金额时点外的各事实阈值及白名单证明。 |
| UNKNOWN-U2 | TD 负责人验证多版本同实体同日的物理隔离、原子可见、tag/列宽及版本标记，避免同 ts 覆写。 |
| UNKNOWN-U3 | 数据负责人确认日K唯一获取方及多源切换发布边界。 |
| UNKNOWN-U4 | runner 核查现行日K Redis 键、值及 TTL。 |
| UNKNOWN-U5 | runner 核查复权原始每字段类型、样例、事件覆盖及转换。 |
| UNKNOWN-U6 | 数据负责人核查 DDJE 官方单位、净额定义及 ttag 语义；元仅中等置信度。 |
| UNKNOWN-U7 | 编排负责人协调 DDE registry 15:12 与 timeline 21:00。 |
| UNKNOWN-U8 | runner 核查竞价与 09:45 真实单行、选点、源时间与覆盖。 |
| UNKNOWN-U9 | runner 核查板块成员唯一来源、字段、历史版本与真实行。 |
| UNKNOWN-U10 | runner 核查股票池唯一来源、字段、历史覆盖与真实行。 |
| UNKNOWN-U11 | runner 核查涨停池/明细完整 DDL、字段、Redis、真实行与来源切换语义。 |
| UNKNOWN-U12 | 指标细则负责人冻结公式、初始化、状态、每指标预热与容忍误差。 |
| UNKNOWN-U13 | Guardian 负责人设计最终失败降级 schema 与运行时解码。 |
| UNKNOWN-U14 | 迁移负责人列出报告特征及两套 stock_day_facts 消费者，核实切换条件。 |

## §2.9 L0 建表与样例补充

以下是设计 DDL，尚未执行；列类型由 C784 DESCRIBE、C786 字符串响应和 `td_sink.py` / `daily_dde.py` 的现行 DDL 推导，版本、因子和白名单列是设计字段，不代表上游原字段。部署须经 DDL 审批；U2 未解决之前不得声称版本可共存或原子切换。无证据的 2.6–2.8 DDL、Redis 值与真实行继续标 UNKNOWN，不以假数据填充。

```sql
CREATE TABLE IF NOT EXISTS core_data.daily_kline_versions (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), rank_level INT, needs_upgrade BOOL, source VARCHAR(96), provider VARCHAR(32), degraded BOOL, quality VARCHAR(4000), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), synced_at VARCHAR(40), schema_version INT);
CREATE STABLE IF NOT EXISTS core_data.adjust_factor_v1 (ts TIMESTAMP, version VARCHAR(24), event_count INT, sha256 VARCHAR(64), events_json VARCHAR(60000)) TAGS (symbol VARCHAR(8));
CREATE STABLE IF NOT EXISTS core_data.auction_amount_facts_v1 (ts TIMESTAMP, amount_yuan BIGINT) TAGS (symbol VARCHAR(8), slot VARCHAR(4));
CREATE TABLE IF NOT EXISTS core_data.auction_amount_facts_days (ts TIMESTAMP, trade_date VARCHAR(10), slot VARCHAR(4), row_count INT, source_note VARCHAR(1024), written_at TIMESTAMP);
CREATE STABLE IF NOT EXISTS core_data.slot0945_amount_v1 (ts TIMESTAMP, amt_yuan BIGINT, version VARCHAR(24)) TAGS (symbol VARCHAR(20));
CREATE TABLE IF NOT EXISTS core_data.fact_days_v1 (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), quality VARCHAR(4000), missing_whitelist VARCHAR(4000));
```

日标记每事实独立物理表，`fact_days_v1` 为列模板，避免多个事实同 ts 冲突；`missing_whitelist` 为 JSON 数组，元素的 symbol 和 reason 按 DECISION-4，长度不足则拒绝提交，不截断。标记最后发布，未知阈值不标完整；按日K现行表迁移白名单须新增列或另表，物理隔离和原子性 U2 待 TD 负责人验证。`adjust_factor_v1` 每股一行保存全部事件的 JSON 数组，ts 为处理时刻，取最新行；空数组为已处理但因子不可得，无行为未处理。事件日超出 TD KEEP 时间范围且会自然过期，故使用单行原子替换；U2 对此事实不再成立。超过 KEEP 未刷新的股票仍会消失，需未来定期全量刷新。部署前 DDL 已由大脑审批。

C778 分支接口合同：`ensure_schema()` 建表；`complete_days(days, slot)` 返回 row_count≥4000 的日期集合；`write_day(day, slot, amounts, source_note)` 在少于4000时抛 `AUCTION_FACTS_INCOMPLETE:<day>:<slot>:<n>` 且不写；校验六位 symbol 和整数后写数据、最后写标记，返回行数；`read_days(days, slot)` 返回日期到 symbol→金额映射，读失败严格抛错，禁止当空。slot 为 `0925`/`0945`。分支实际采用共表并覆盖旧行、先删标记，不能满足 U2 版本隔离；迁移需先实现隔离，再核对同日替换和故障恢复，再切换消费者。`auction_amount_v1` 与 `slot0945_amount_v1` 是逻辑分视图，需确定与 C778 共表的唯一权威关系，不能双写形成两个真相源（UNKNOWN，数据负责人）。源时间和精确选点 UNKNOWN-U8，不得把交易日零点误当采集时间。

真实样例（C784/C788）：日K TD 行 `2026-11-06,600519,close=1453,volume_shares=4284772,amount_yuan=6199528289.3,turnover_pct=0.3428`；DDE 配对 `2026-10-21,300750,ddje=-1340525000,amount_yuan=32439007670.33`；因子 `600519,2025-06-26,foreAdjustFactor=0.960527`（C786）。现行 Redis 的实际完整 JSON 行 UNKNOWN（需 runner 用已保存证据核实）；以下仅为数据字段格式示例，数值来自 C788，非实测 Redis 原文：
```json
{"symbol":"300750","ddje":-1340525000}
```
竞价、0945、成员、universe、涨停池/明细的完整真实 TD 行与 Redis JSON 仍 UNKNOWN（需对应数据负责人从采集产物确认）；不得把局部数值视为完整行。

## §3.1 价量簇具体 DDL 与预热

设计示例（列名及状态为新增合同，类型按指标 DOUBLE 与 §3 元列；物理版本隔离待 U2）：
```sql
CREATE STABLE core_data.price_volume_indicators_v1 (ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), input_refs NCHAR(16384), available_at VARCHAR(40), quality VARCHAR(16), warm BOOL, ma5 DOUBLE, ma10 DOUBLE, ma20 DOUBLE, ma60 DOUBLE, ema12 DOUBLE, ema26 DOUBLE, dif DOUBLE, dea DOUBLE, macd DOUBLE, k DOUBLE, d DOUBLE, j DOUBLE, rsi6 DOUBLE, rsi12 DOUBLE, rsi24 DOUBLE, boll_upper DOUBLE, boll_middle DOUBLE, boll_lower DOUBLE, boll_bandwidth DOUBLE, atr14 DOUBLE, pct_chg_volatility20 DOUBLE, state_ema12 DOUBLE, state_ema26 DOUBLE, state_dea DOUBLE, state_k DOUBLE, state_d DOUBLE, state_rsi6 DOUBLE, state_rsi12 DOUBLE, state_rsi24 DOUBLE, state_atr14 DOUBLE) TAGS (symbol VARCHAR(8));
```
状态列只列出递推值；初始化与状态充分性须细则卡核实。示例引用范围及版本为合同格式，不是已发布真实版本：
```json
[{"dataset":"daily_kline","start_date":"2026-05-18","end_date":"2026-11-06","version":"example"},{"dataset":"adjust_factor","start_date":"2026-05-18","end_date":"2026-11-06","version":"example"}]
```
名称/默认参数：MA 5/10/20/60；EMA 12/26；MACD 12/26/9；KDJ 9/3/3；RSI 6/12/24；BOLL 20/2；ATR14；涨跌幅波动率20（任务 C789）。最低窗口：MA 分别 5/10/20/60，BOLL 20，波动率20 为20个有效涨跌幅（按名称中的窗口长度）；EMA12/26、MACD、KDJ、RSI、ATR 的初始化与最小预热 UNKNOWN（需价量细则卡，不能把参数直接当初始化证明）。暖机不足 PARTIAL 且 warm=false；公式、缺失与误差留细则卡。

## §4.1 registry 完整声明示例

现行 `datasets.registry.json` 顶层 `items` 是对象数组；每个元素以 `id` 标识，`requires`/`produces` 是新增的同级字段，当前运行时不识别，须实现并验证 schema（该文件及 `chain_sweep.py`）。以下是设计片段，不能声称已有支持；窗口数为交易日：
```json
{"id":"price_volume_indicators","requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"},{"dataset":"adjust_factor","window_days":120,"min_quality":"FORMAL"}],"produces":["price_volume_indicators"]}
{"id":"chip_indicators","requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"},{"dataset":"adjust_factor","window_days":120,"min_quality":"FORMAL"}],"produces":["chip_indicators"]}
{"id":"dde_indicators","requires":[{"dataset":"daily_dde","window_days":1,"min_quality":"FORMAL"},{"dataset":"daily_kline","window_days":1,"min_quality":"FORMAL"}],"produces":["dde_indicators"]}
{"id":"plate_amount_window","requires":[{"dataset":"auction_amount","window_days":64,"min_quality":"FORMAL"},{"dataset":"slot0945_amount","window_days":64,"min_quality":"FORMAL"},{"dataset":"plate_members","window_days":64,"min_quality":"FORMAL"}],"produces":["plate_amount_window"]}
```
复权事件不是逐日行，检查覆盖窗口起点之前最近事件及整股快照版本，不能要求每日各有事件。120 是最大计算窗口，不意味着筹码不足120根不能计算，最少5根且按质量规则处理。

## §9.1 timeline 填写示例与触发

以下各块是 `timeline.json` 顶层现有 `functions` 对象、`timeline` 数组与 `policies` 对象的设计示例，字段结构参照 `plate_amount_baseline_0815` / `policy_12`（C774）；handler 与 item 接线、确切时间、最终失败降级均未实现。示例 21:30 晚于 Baostock 17:30–18:30 窗口；DDE 21:00 调度和 registry 15:12 冲突 U7，因此仍由依赖就绪决定，不保证21:30 已完成。
```json
{"functions":{"price_volume_indicators":{"after":[],"args":{"item_id":"price_volume_indicators"},"available_after":null,"calendar":"trading_day","cost":"medium","data_date_offset":0,"depends_on":[],"enabled":true,"gate":"calendar_ready","handler":"fetch_item","late_until":"23:30:00","on_deadline":"degrade","on_phase_mismatch":"defer","order":null,"phases":["NIGHT"],"trigger_type":"window","policy":"policy_12"},"chip_indicators":{"after":[],"args":{"item_id":"chip_indicators"},"available_after":null,"calendar":"trading_day","cost":"medium","data_date_offset":0,"depends_on":[],"enabled":true,"gate":"calendar_ready","handler":"fetch_item","late_until":"23:30:00","on_deadline":"degrade","on_phase_mismatch":"defer","order":null,"phases":["NIGHT"],"trigger_type":"window","policy":"policy_12"},"dde_indicators":{"after":[],"args":{"item_id":"dde_indicators"},"available_after":null,"calendar":"trading_day","cost":"medium","data_date_offset":0,"depends_on":[],"enabled":true,"gate":"calendar_ready","handler":"fetch_item","late_until":"23:30:00","on_deadline":"degrade","on_phase_mismatch":"defer","order":null,"phases":["NIGHT"],"trigger_type":"window","policy":"policy_12"}},"timeline":[{"at":"21:30:00","run":[{"fn":"price_volume_indicators","id":"price_volume_indicators_2130","deadline":"23:30:00"},{"fn":"chip_indicators","id":"chip_indicators_2130","deadline":"23:30:00"},{"fn":"dde_indicators","id":"dde_indicators_2130","deadline":"23:30:00"}]}],"policies":{"policy_12":{"catchup":"run_in_window","retry_backoff":"","retry_until_late":false,"silent_until_last":false,"ladder":[{"name":"legacy","attempts":1,"backoff":"","backoff_cap":"5m","timeout":"540s","budget_enabled":false}]}}}
```
日K转 FORMAL 后应经 `chain_sweep` 检查依赖并安排受影响簇，夜间事件用于兜底；这是新增接线要求，并非现行 sweep 已自动触发这些簇。三簇各自检查全部依赖与 as-of；`on_deadline=degrade` 不等于最终失败自动降级（C774），最终失败动作见 U13。

## §10.1 迁移顺序与删除条件

1. 冻结事实与算法细则、U2 版本隔离；验证真实样本和 DDL 评审；旧路径不删除。
2. 部署因子与金额唯一获取方、日标记；验证实际覆盖、版本回读和失败无完整标记；旧直读暂留。
3. 回填足够历史及因子快照；验证64日覆盖与真实除权样本；旧直读暂留。
4. 部署各簇并双读最短真实窗口，核对血缘及质量；消费者未迁完不得删除旧派生路径。
5. 切换编排和消费者；验证 FORMAL sweep、夜间重试与 as-of；消费清单通过后可删除 `auction_baseline` 和 `plate_amount_baseline` 的对应 market_data1 直读路径。
6. 完成回滚期及字段级消费核对；验证报告、stock_day_facts 与 ladder 输出；可删除已迁完的旧 `daily_indicators`、相关 `daily_report_features` 构建路径，未迁字段及独立 CSV 路径保留（U14）。

## 附录 A:变更记录

C789：落定六项决策；补充 C784/C786/C787/C788 证据、目标 DDL、真实值、价量列、依赖和编排示例、迁移顺序与验证边界。所有新增结构均为设计合同，不代表部署。

## 附录 B:未决项清单

U1–U14 见 §12，责任分别为数据负责人、TD 负责人、数据负责人、runner、复权实现卡、数据负责人、编排负责人、runner、板块数据负责人、universe 数据负责人、涨停数据负责人、价量细则卡、Guardian 负责人、迁移负责人。另需：复权实现卡验证新事件前后因子重发与除权迹象阈值/误报、原字符串到 DOUBLE 精度；筹码细则卡核对退化与负换手；数据负责人确定 C778 共表与逻辑分表的唯一权威及全部事实的完整真实样例；编排卡核实新增 handler 与调度时间。无法证明的保持 UNKNOWN。
