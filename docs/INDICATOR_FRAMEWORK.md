# 指标框架横切规范

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。状态：设计合同；标为“拟新增”的字段、表、命令和行为不是现行实现。未决项阻止相应细则和实现宣称完成。来源缩写：C784 = `/home/exedev/fw/reports/C784.md` 及 `/home/exedev/validation/realdata/indicator_samples_20261107/REPORT.md`；C782/C783/C774 = `/home/exedev/fw/reports/` 对应报告；代码路径相对仓库根。示例若明确标“合同示例”，只说明格式，不宣称是采集数据。时间均为 Asia/Shanghai，as-of 使用带时区时间。

## §0 目的与总原则

- 已收盘交易日每项事实只有一个获取方；首次成功发布后消费者复用，修订由获取方发布新版本。指标函数不得直接读取 market_data1 大表或外部接口。需要显式拉取时遵守 §4 租约。
- 按依赖和失效边界分簇分表；同簇同一输入版本一次遍历，共用中间量只计算一次。输入及算法版本未变时不再计算；不同函数共用 L0，不能各自获取。
- 日K L0 不复权（Baostock `adjustflag=3`，`src/engine_core/daily_history.py:536`）；复权事件独立存储；价量和筹码簇使用前复权价，算法版本记录复权口径。新除权事件使该股全部历史递推指标失效，从头重算。
- 新金额列 BIGINT 整数元；指标及比率 DOUBLE；缺失 NULL，不补零。旧日K金额 DOUBLE 及实际小数的转换须先解决 DECISION-6，不得静默截断。
- TD `core_data` 为冷层和唯一真相源，保留 3650 日；Redis `core:ind:` 仅缓存最近一日及最近 N 日最新值，默认 TTL 604800 秒；丢失从 TD 重建，不触发上游。market_data1 数据库 KEEP 60 日，64 日基线的原始事实必须先存 core_data（C784）。
- 预热不足必须 `quality='PARTIAL'`、`warm=false`；未达完整性门槛的依赖默认不计算。每个函数可独立运行并自行检查依赖。

## §1 术语与分层

L0 是有来源、时点和版本的市场事实，不是策略结论；L1 是依赖已发布事实计算出的指标和状态；L2 是报告/筛选消费，不反写事实。簇是输入、触发和失效边界兼容的一组 L1；获取方是某项 L0 的唯一发布者。版本标识输入内容及来源，`available_at` 是消费者可见时刻，不能用未来可见的版本回答历史 as-of。交易日历决定窗口，不用自然日代替。

## §2 L0 事实清单

以下每节的 1–11 顺序固定：名称、含义、获取方/接口、时点频率、增量/水位、TD/DDL、日标记、Redis、真实样例、代码、差距。字段来源以小节括号内来源统一标注，拟新增字段另注明。日标记设计复用 `daily_kline_versions` 的 `trade_date/version/business_status/row_count/sha256/available_at`（`td_sink.py:302`）；新增事实的完整门槛、缺停牌和退市处理均为 UNKNOWN-U1，未冻结之前不能发布完整标记。日标记在数据写完、核对行数与摘要后最后发布；缺行或未知阈值不得标完整。所有拟新增表须单独经过 DDL 审批；下列 DDL 是设计文本，不是已执行事实。`ts` 为交易日上海时区零点转换的时间戳；事件事实以事件日为 ts。拟新增热视图统一 `core:fact:{fact}:{YYYY-MM-DD}`，hash field 为实体，value 为带版本的 JSON，TTL 604800 秒；现状如有不同另列。数据修订不得覆盖已有可见版本：物理隔离方式见 UNKNOWN-U2。

### 2.1 daily_kline（来源 C784；`src/engine_core/daily_history.py:23,536`；`src/engine_core/data_pipeline/td_sink.py:147,295`）

1. 名称：`daily_kline`。2. 不复权日 OHLC、成交与状态。3. 现行日K获取链使用 Baostock `query_history_k_data_plus`；请求字段 `date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,tradestatus`，`adjustflag=3`；唯一发布方及多来源切换边界待 UNKNOWN-U3。4. 收盘后按交易日增量。5. 按股、交易日与已发布版本检查，缺口按交易日补齐，不以最大日期证明全市场完整。6. 实测表（金额现为 DOUBLE）：
```sql
CREATE STABLE IF NOT EXISTS core_data.daily_kline_v1 (ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE, pct_chg DOUBLE, volume_shares BIGINT, amount_yuan DOUBLE, turnover_pct DOUBLE, suspended BOOL, version VARCHAR(24)) TAGS (symbol VARCHAR(8));
```
7. 现行 `core_data.daily_kline_versions` 列与类型见 C784；完整阈值 U1。8. 新热视图按本节共同规则，现行视图精确键 UNKNOWN-U4。9. 真实样本：2026-11-06 `symbol=600519, close=1453, volume_shares=4284772, amount_yuan=6199528289.3, turnover_pct=0.3428`（C784）。10. 代码见标题。11. 与整数元目标冲突，不能改变既有表事实；小数处理 DECISION-6。

### 2.2 adjust_factor（来源 C784）

1. `adjust_factor`。2. 除权事件因子，非逐日记录。3. Baostock `query_adjust_factor`；真实字段 `code,dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFactor`，类型及原始字符串到数值转换 UNKNOWN-U5；获取方拟新增。4. 收盘后及发现新事件时检查。5. 每股以事件日和来源版本去重，不能把无事件日当作缺失；阶梯累计和基准见 DECISION-3。6. 拟新增，类型待 U5，DDL 不得伪造：`CREATE STABLE core_data.adjust_factor_v1 (ts TIMESTAMP, ... UNKNOWN-U5 ...) TAGS (symbol VARCHAR(8));`。7. 共同日标记，完整性须能证明查询覆盖目标股和日期，阈值 U1。8. 共同热视图；不足以替代冷层事件历史。9. 真实样本：120 个交易日三股分别返回 1、2、2 行（C784）；具体事件值 UNKNOWN-U5。10. 新增。11. 因子阶梯方向、基准及零事件证明未验证，不可实现前复权。

### 2.3 daily_dde（来源 C784；`src/engine_core/data_pipeline/daily_dde.py:78,196,424`；`src/engine_core/guardian/datasets.registry.json:212`）

1. `daily_dde`。2. 每股每日 DDJE 原值。3. 现行 `run_daily_dde_sync` 调用开盘啦 `getHisStockDDE`，响应 `Date` 为 YYYYMMDD 字符串数组、`DDJE` 整数数组、`ttag` 浮点、`errcode` 字符串；`ttag` 含义未知，DDJE 单位 UNKNOWN-U6，尽管 Core 标注元。4. 盘后；registry 15:12 与 timeline 21:00 冲突，见 U7。5. 每股水位补缺，拒绝源最新日早于目标日，不能把空值当零。6. 实测：
```sql
CREATE STABLE IF NOT EXISTS core_data.daily_dde_v1 (ts TIMESTAMP, ddje DOUBLE, version VARCHAR(24)) TAGS (symbol VARCHAR(8));
```
7. 拟新增共同日标记，门槛 U1。8. 现行 `core:cache:data:daily_dde:{date}` hash，symbol→JSON，TTL 86400 秒；不是新指标热键。9. 2026-11-06 三股各返回最近 30 日，与 TD 90/90 相等（C784）；单行数值 UNKNOWN-U6。10. 见标题。11. 无 `large_net/ddx/ddy/ddz` 来源；不能编造；比率须先核实单位及分母非零。

### 2.4 auction_amount（来源 C784；C782；分支 `C778-auction-amount-facts-store` 的 `src/engine_core/data_pipeline/auction_amount_facts.py`）

1. `auction_amount`。2. 09:25 竞价匹配金额。3. 唯一获取方拟从 `market_data1.auction_snapshot_v2` 提取，原字段 `ts,match_amt_yuan,symbol,trade_date,auction_tag`（C784）；C778 未合并，其接口不能当作已上线。4. 09:25 后可用时逐交易日抓取一次。5. 每日校验目标股及固定时点，保存实际源时间，补历史仅由获取方进行。6. 拟新增冷表：
```sql
CREATE STABLE core_data.auction_amount_v1 (ts TIMESTAMP, match_amt_yuan BIGINT, version VARCHAR(24)) TAGS (symbol VARCHAR(20));
```
`ts` 为交易日零点；精确选点及源时间字段 UNKNOWN-U8，DDL 尚不能投入生产。7. 共同日标记，门槛 U1。8. 共同热视图。9. 真实单行值 UNKNOWN-U8（需 runner 从 C784 原始响应核对）；不得以构造值代替。10. C778 分支；现行 `src/engine_core/auction_baseline.py:188` 仍读 TD。11. 60 日源保留不足 64 日，冷存必须先建并回填。

### 2.5 slot0945_amount（来源 C784；`src/engine_core/plate_amount_baseline.py:130,193`）

1. `slot0945_amount`。2. 09:45 时点累计成交金额。3. 拟新增唯一获取方读取 `market_data1.stock_tick_v2` 的 `ts,amt_yuan,symbol`；`amt_yuan BIGINT`（C784）。4. 09:45 后逐交易日。5. 时间边界与选点规则 UNKNOWN-U8，不得把竞价虚拟匹配额加到累计成交额上。6. 拟新增：
```sql
CREATE STABLE core_data.slot0945_amount_v1 (ts TIMESTAMP, amt_yuan BIGINT, version VARCHAR(24)) TAGS (symbol VARCHAR(20));
```
源时间保留和选点依赖 U8。7. 共同日标记，门槛 U1。8. 共同热视图。9. 真实单行值 UNKNOWN-U8。10. 见标题。11. 现行基线直接读 tick，需先迁到唯一 L0 获取方。

### 2.6 plate_members（来源 `src/engine_core/plate_amount_baseline.py:49,292`；C782）

1. `plate_members`。2. 某交易日板块与股票关系。3. 已有消费者接受 `plate_code` 和 `symbol`；唯一获取方及实际源 UNKNOWN-U9。4. 每交易日形成可追溯快照；历史查询禁止使用当前关系冒充历史。5. 按日版本与成员集合摘要判定变化。6. 拟新增 DDL 物理主键和 tag 设计 UNKNOWN-U2/U9：`CREATE STABLE core_data.plate_members_v1 (ts TIMESTAMP, ... UNKNOWN-U9 ...) TAGS (... UNKNOWN-U2 ...);`。7. 共同日标记，覆盖与阈值 U1。8. 共同热视图，实体必须包含板块和股票，编码 U9。9. 真实一行 UNKNOWN-U9。10. 见标题。11. 历史快照覆盖与来源需 runner 核查。

### 2.7 symbol_universe（来源 `src/engine_core/data_pipeline/derived_daily.py:189`；C782）

1. `symbol_universe`。2. 每日应覆盖股票集合。3. 已有血缘引用 `dataset,trade_date,version,business_status`；唯一获取方与源字段 UNKNOWN-U10。4. 按交易日快照。5. 以日期、版本与成员摘要校验，不能以前一日最大水位替代。6. 拟新增 DDL：`CREATE STABLE core_data.symbol_universe_v1 (ts TIMESTAMP, ... UNKNOWN-U10 ...) TAGS (... UNKNOWN-U2 ...);`，不得执行占位文本。7. 共同日标记，上市、退市、停牌规则 U1。8. 共同热视图。9. 真实一行 UNKNOWN-U10。10. 见标题。11. 与各源可查询股票集合的差异必须留缺口证据。

### 2.8 limit_up_pool/detail（来源 C782；`src/engine_core/guardian/datasets.registry.json:437,2156`）

1. `limit_up_pool` 与 `limit_up_detail` 两个事实。2. 每日涨停池及明细。3. 池现有来源开盘啦→问财→TD 日K，明细依赖池并取开盘啦；各唯一发布方及源字段 UNKNOWN-U11。4. 池取 D-1，分别 00:40、00:50 后触发（C782）。5. 逐日检查版本、池明细关系；明细现缺日默认回补 20 日。6. 现有池表 `core_data.limit_up_pool_v1` 与 `limit_up_pool_versions`（C783）；列和 detail 表 DDL UNKNOWN-U11：`CREATE STABLE core_data.limit_up_pool_v1 (ts TIMESTAMP, ... UNKNOWN-U11 ...) TAGS (... UNKNOWN-U11 ...);`；detail 同理，不得执行占位文本。7. 各自标记与覆盖阈值 U1，不以池非空证明明细完整。8. 各自已有 Redis 视图的键与 TTL UNKNOWN-U11；新视图共同规则。9. 真实一行 UNKNOWN-U11。10. 见标题。11. 统一血缘、降级源语义及完整性待核对。

## §3 L1 通则

一簇一表：逻辑键 `(cluster, entity, trade_date, version)`，每个计算结果与递推状态必须原子可见。TD 的同子表同 ts 覆写会丢版本；实际子表/version 隔离方案 U2 冻结前不得实现。固定元列 `version,algo_version,input_refs,available_at,quality,warm`（拟新增；版本与血缘参考 `derived_daily.py:189,208`）；状态列随簇定义，不能只保存在进程内。`input_refs` 按 dataset、trade_date 排序的 JSON，例（格式示例，字段见 `derived_daily.py:189`）：
```json
[{"dataset":"daily_kline","trade_date":"2026-11-06","version":"example","business_status":"FORMAL"}]
```
SHA-256 对规范 UTF-8 JSON、算法版本及参数计算；版本缺失或不可验证时不跳过。金额 BIGINT 元；价量指标 DOUBLE，未知 NULL。预热不足保留可计算值但 PARTIAL/false；缺关键输入不发布结果。通用 DDL 模板（`<...>` 为设计占位，非可执行 SQL；列宽与 tag 映射需 U2）：
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

按日期升序，先解析同一份前复权输入，再每股每根 K 线一次更新滚动高低点、均值/方差、EMA、K/D 及各指标；各中间量在同簇仅更新一次。递推状态拟用同版本同日 DOUBLE 列 `ema_12,ema_26,macd_signal,k,d`（§3 拟新增），公式、初始值和精度 U12；必须从前一有效检查点恢复，检查点血缘不匹配则回退。窗口型读取或保存完整窗口；中位数/分位数不能只保存和与计数。筹码分布必须持久化桶边界、权重和编码版本，但桶数、精度、序列化与衰减为 DECISION-1，未决前不可实现。一次性事实按实体日检查已发布完整版本并复用；修订另发版本。

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

拟新增指标 hash：`core:ind:{cluster}:{YYYY-MM-DD}`，field 为实体，value 为 JSON；TTL 604800 秒。例如合同格式（拟新增字段）：`{"version":"example","algo_version":"example","available_at":"2026-11-06T21:00:00+08:00","quality":"PARTIAL","warm":false,"close":1453.0}`，其中 close 的真实值来自 C784，其余是格式示例。N 由 DECISION-5 确定。TD 现有表 `daily_kline_v1,daily_kline_versions,daily_dde_v1`；拟新增 §2 事实表、每簇表和各自完整标记，物理版本隔离 U2。Redis 清空后按 as-of 从 TD 读取最近合格版本、核对标记并重建 hash/TTL；TD 无合格数据返回缺依赖，绝不回源。生产路径禁止中间文件作为事实交换或权威存储（fw/design/31）；已有 catalog 迁移期间仅作为兼容读取，退出条件见 §10。

## §8 函数契约

拟新增入口 `run(date, window, pull_upstream=False, force=False, dry_run=False, available_at=None, td_split_retry_depth=None) -> (exit_code, reply)`；CLI 为 `--date --window --pull-upstream --force --dry-run --available-at`，只有适用 TD 拆分读取的获取方接受 `--td-split-retry-depth`（现有 `plate_amount_baseline.py:161`）。`--force` 重算但不能绕过依赖及租约；dry-run 不写入、不拉源。相同输入引用、算法与参数及完整提交是幂等键。输出遵循 `src/engine_core/guardian/contract.py:14`，所有代码须同时登记到 `error_codes.json` 与运行时矩阵。

| exit | 状态 | error_code | 登记情况/语义 |
|---|---|---|---|
| 0 | ok | 空字符串 | 成功；`contract.py:32` |
| 10 | degraded | `STALE_VERSION` | 拟新增；使用合格旧版本，非重试 |
| 20 | not_applicable | `ALREADY_SATISFIED` | 已登记；幂等跳过 |
| 30 | failed | `NOT_YET_AVAILABLE` | 拟新增；依赖未就绪，可重试 |
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
| daily_dde | 保留 L0 获取方；派生归 DDE 簇 | registry 15:12 与 timeline 21:00 不一致，U7；单位 U6 |
| auction_baseline | auction_amount L0 后迁窗口簇 | 先冷存足够历史并验证 fallback 语义，消费者迁完再删直读 |
| plate_amount_baseline | 两种金额事实及成员 L0 后迁窗口簇 | 先持久化 64 日，保留历史成员版本与 as-of 降级；消费者迁完再删直读 |
| C778 auction_amount_facts | 审核未合并分支作为 L0 候选 | 接口及存储必须与本规范核对；合并与切换前不得当现行代码 |

该分支 `auction_amount_facts.py:13` 提供 `ensure_schema/complete_days/read_days/write_day`；其 `auction_amount_facts_v1` 为 `ts,amount_yuan BIGINT`，tag 为 `symbol,slot`，日表为 `auction_amount_facts_days`。`write_day` 先删除完成标记和旧行再写入；它没有本规范要求的不可覆盖版本隔离和输入血缘，不能直接视作满足目标。其 0925/0945 共表方案还须按事实独立完成与失效边界评审。

迁移顺序：冻结决策和事实细则 → 唯一获取/冷存/标记 → 真实回填和依赖协议 → 簇增量与双读对账 → 切片及编排切换 → 经消费清单、真实窗口验证和回滚期后删除旧路径。旧算法迁移先 fixture/evidence，禁止导入或复制旧项目源码。

## §11 验证策略

每张实现卡先写因旧行为失败的最小测试；解析与计算只回放 `examples/capture_real_fixture.py` 抓取并登记 `tests/fixtures/real/PROVENANCE.json` 的真实原始响应，不能手写假行。C784 的 `baostock_raw.jsonl`、`kaipan_raw.jsonl`、`td_raw.jsonl` 是研究证据，需 runner 按项目捕获流程重新采集/登记后才可当测试夹具；不要直接冒充已登记夹具。新数据源加 `pytest --live` 契约测试。因子由 runner 抓跨除权事件的原始记录和供应方口径；金额、成员、股票池、涨停明细由 runner 抓带时间与来源的真实响应（U8–U11）。

每类函数至少验证：L0 同日第二次不调用上游、缺行不发布完整；L1 缺一个日期返回 30 且 detail 指明事实日期；递推接续与同一真实短窗口从头计算一致；窗口含修订日的结果重算，其他窗口保持版本；新除权事件使该股全部历史递推重算，其他股票不变；warm 不足为 PARTIAL/false；金额转换按 DECISION-6。使用真实 TD 结果重建被隔离的测试 Redis 热键，核对版本、值和 TTL，并断言无上游调用；不能删除非 `core:` 键。as-of 降级不能读到未来版本。验证最短可触发窗口，记录原始命令与输出及 fixture 来源，不用全量回放冒充覆盖。

## §12 决策点与 UNKNOWN 清单

| 编号 | 选项、倾向、影响 |
|---|---|
| DECISION-1 | 筹码峰：C591 旧算法兼容或新模型；倾向先证明旧算法真实样本再决定。冻结衰减系数、价格分箱桶数/精度、权重编码、换手及所需字段；影响状态格式和全历史回算。 |
| DECISION-2 | DDE 仅 DDJE，或增加有证据的派生（如 ddje/amount_yuan）；倾向先仅原值，确认单位后再加比率；无 large_net/ddx/ddy/ddz 来源。 |
| DECISION-3 | 前复权基准：最新交易日归一或供应方其他基准；倾向先由 runner 用跨事件样本与供应方规则核验再拍板，事件阶梯方向亦未定；影响全历史。 |
| DECISION-4 | 各事实完整阈值及停牌/退市处理：严格全覆盖或有证据的缺口白名单；倾向按事实制定带原因的白名单，不把缺失记零；影响发布与可用性。 |
| DECISION-5 | Redis 最近 N 日：小窗口或覆盖最长消费窗口；倾向按消费清单确定最小 N，默认 TTL 7 日不变；影响内存和命中。 |
| DECISION-6 | 上游小数元转整数：四舍五入、截断或保留更小单位；倾向明确舍入及误差审计后使用整数元，需用户决定；影响已有 DOUBLE 数据与对账。 |

| 编号 | 待查及责任 |
|---|---|
| UNKNOWN-U1 | runner 与数据负责人核对每个事实完整性阈值、停牌/退市及空日证明；交 DECISION-4。 |
| UNKNOWN-U2 | TD 负责人验证多版本同实体同日的物理隔离、原子可见、tag/列宽及版本标记，避免同 ts 覆写。 |
| UNKNOWN-U3 | 数据负责人确认日K唯一获取方及多源切换发布边界。 |
| UNKNOWN-U4 | runner 核查现行日K Redis 键、值及 TTL。 |
| UNKNOWN-U5 | runner 核查复权原始每字段类型、样例、事件覆盖及转换。 |
| UNKNOWN-U6 | runner 核查 DDJE 单位、ttag 语义及单行真实值；单位未定前不得声称比率有效。 |
| UNKNOWN-U7 | 编排负责人协调 DDE registry 15:12 与 timeline 21:00。 |
| UNKNOWN-U8 | runner 核查竞价与 09:45 真实单行、选点、源时间与覆盖。 |
| UNKNOWN-U9 | runner 核查板块成员唯一来源、字段、历史版本与真实行。 |
| UNKNOWN-U10 | runner 核查股票池唯一来源、字段、历史覆盖与真实行。 |
| UNKNOWN-U11 | runner 核查涨停池/明细完整 DDL、字段、Redis、真实行与来源切换语义。 |
| UNKNOWN-U12 | 指标细则负责人冻结公式、初始化、状态、每指标预热与容忍误差。 |
| UNKNOWN-U13 | Guardian 负责人设计最终失败降级 schema 与运行时解码。 |
| UNKNOWN-U14 | 迁移负责人列出报告特征及两套 stock_day_facts 消费者，核实切换条件。 |
