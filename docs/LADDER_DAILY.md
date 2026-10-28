# ladder_daily (C577)

连板梯队日表。`ladder_v1` 取代 2026-10-28 之前的"接力累加 + 收盘价代理"算法(C574 证实 10-27 分区全错:
002724 记 6 连板实为 1,600838 记 4 实为 5)。设计依据 `fw/design/24-DATASET-CONTRACT.md`,规格 `dataset_specs/ladder_daily.json`。

## 1 结构

| 层 | 文件 | 职责 |
|---|---|---|
| 纯函数 | `data_pipeline/ladder_daily.py` `build_ladder_daily(request) -> Partition` | 不读环境/磁盘/网络/时钟,不读"上一个 ladder 分区"。 |
| 适配层 | `ladder_daily_adapter.py`、`examples/build_ladder_daily.py` | 读 catalog、按需加载输入(`NeedInput` 驱动)、节流的 Kaipan/问财、落盘、可选同步 TD。退出码 0/20/30/40。 |
| 存储 | catalog 分区(过渡账本)+ TD `ladder_daily_v1` / `ladder_daily_versions`(`data_pipeline/td_ladder_sink.py`,复用 C567 的连接/版本秩/日时间戳) | 冷数据 -> TD;无 Redis 热视图(结果只在盘后一次产出,无盘中消费方,见 7)。 |
| 触发 | registry `ladder_daily.derive` + `reconcile_plan` 派生分支(与 C571 同机制) | 事件门控:当日 `daily_kline` >= PROVISIONAL 落地即派生;输入版本变化 -> 新版本;启动晚于触发点由 `boot_apply_plan` 回补。 |

## 2 算法

逐日取涨停集合,**窗口内无状态**:

1. D 日集合 S_D;每只 S_D 内的票 streak=1。
2. 沿交易日历往前一天一天取 S_d,`alive = alive ∩ S_d`,命中者 streak+1、`streak_start_date=d`;`alive` 为空即停(窗口 = "刚好数出最长连板所需的交易日",上限 `window_days` 默认 20,可配)。
3. 某日 S_d 为 UNKNOWN(所有来源都答不了):仍存活的票 `streak_n` 是**下限**,行 `quality="LOWER_BOUND:unknown_day:<日期>"`,`business_status=DEGRADED`,`quality.partial=true`,`quality.chain_truncated_days` 记日期。不编数。
4. 触到窗口上限:`LOWER_BOUND:window_cap`(`quality.window_capped`)。
5. D 日本身 UNKNOWN -> 不产分区(`UpstreamNotReady LIMIT_UP_SET_UNKNOWN`,退出码 20)。

行(稀疏,仅 D 日涨停的票,`symbol` 排序):`symbol, date, trade_date, limit_up(True), streak_n, streak_start_date, day_source_chain("a>b>c",D 起往前每日的来源), quality("OK"|"LOWER_BOUND:...")`。`date` 是契约必有列,与 `trade_date` 同值。

## 3 逐日来源阶梯(配置驱动)

registry `ladder_daily.derive.source_ladder = ["daily_kline","kaipan","wencai"]`,Python 不写死顺序;配置里没有的 rung 永不尝试。

| rung | 条件 | 来源标识 / 质量 |
|---|---|---|
| a `daily_kline` | 该日分区存在(>=PROVISIONAL),**前一交易日分区也存在**,且该日 `pre_close` 与前一日 `close` 一致的比例 >= `continuity_min_ratio`(0.95);行内 `date`(若有)== 该日。任一不满足 -> 该日降级,记 `provider_attempts[].reason`(`previous_day_kline_missing`/`not_continuous:...`) | `derived_daily_kline`,质量 = 日K分区的 business_status |
| b `kaipan` | `limit_up_pool` 分区(`date_basis` 为被描述日,行 `trade_date` 必须等于该日);catalog 没有则 live `getHisBans(date=该日)`(最多 `max_requests` 次,不写 limit_up_pool,NOT_DO) | `kaipan`,质量 = 池分区 business_status(无 -> PROVISIONAL) |
| c `wencai` | "YYYY-MM-DD涨停",列名 `[YYYYMMDD]` 必须等于该日(日期证据,不符 = TIER_DATE_MISMATCH 语义,拒收);持久节流(间隔 >=10s)、每次运行最多 `max_queries_per_run`;cookie 从 `cookie_path` 读,不打印 | `wencai`,质量 DEGRADED |
| d | 都没有 | 该日 UNKNOWN(见 2.3) |

PIT:`available_at` 晚于 `as_of` 的输入不可见。Kaipan 池是次日 ~08:31 才入库,所以同日晚上 D 日的池不可见,
D 日只能靠日K;日K缺失时这一天要等次日池到达(输入变化 -> 重算新版本)。
内置交叉验证:同一天同时有日K结果与 Kaipan 池,`quality.cross_validation[日期]` 记两边只数、交集、各自独有的只数与前 5 个样例,不一致不静默。

## 4 涨停规则(用户 2026-10-28 裁决)

涨幅限制**只由证券所在板块决定,不区分 ST**(ST 5% 已取消):

| 板块 | 判定(代码前缀) | 限制 |
|---|---|---|
| 沪深主板 | 600/601/603/605、000/001/002/003 | 10% |
| 创业板 | 300/301/302 | 20% |
| 科创板 | 688/689 | 20% |
| 北交所 | 43/83/87/88/92 | 30% |
| 其它 | UNKNOWN | **不计入涨停**,`quality.day_stats[d].board_unknown` 记数,绝不默认主板 |

涨停价 = `round_half_up(pre_close x (1+限制), 2)`,涨停 <=> `round(close,2) == 涨停价`(`close` 高于涨停价的行记 `above_limit_not_limit_up`,不计入)。`pre_close` 取行内 `pre_close`(不再用 `close/(1+pct)` 反推)。停牌/成交额 0 的行记 `no_trade`,不计入。

新股例外(查证:联网检索只得到互相矛盾的券商问答,**未取得交易所原文**,因此下表中"已采用"的只是各来源一致的部分):

| 板块 | 规则 | 处理 |
|---|---|---|
| 主板/创业板/科创板 | 注册制新股上市前 5 个交易日无涨跌幅限制(各来源一致) | 上市第 1-5 日不计涨停(`exempt_new_listing`) |
| 北交所 | 首日无限制(各来源一致);第 2-10 日说法分歧(有称前 10 日无限制) | 首日不计;第 2-10 日**不猜**,不计入并记 `bse_rule_unverified` |

上市日来源:`stock_static.listing_date`(最新分区);查不到则由日K推断(该票在前一交易日已验证连续的日K中不存在 -> 首日,往前最多 10 日数上市序号;前后两日有洞则不当新股)。判不出 -> 当普通票(不编例外)。
已知缺口:退市整理期、风险警示以外的特殊制度日等未单独处理;stock_static 只到 10-14,之后上市的新股靠推断。

## 5 manifest

`source`/`provider` = D 日实际来源(`derived_daily_kline`/`kaipan`/`wencai`),`provider_chain` = 窗口内出现过的来源按首次出现排序,`provider_attempts` = 每个被访问日每个 rung 的结果与原因;`degraded` = provider 不是 `derived_daily_kline` 或 status=DEGRADED;
`business_status` = 所有被访问且参与结果的日子里最低的输入质量(链被截断 -> DEGRADED);`quality.history_non_formal_days` 列出非 FORMAL 的历史日(它们升到 FORMAL 后输入版本变化 -> 自动重算新版本)。
`inputs.days{日期:{source,version,business_status,limit_up_count,set_sha}}`、`input_refs`(每个被访问日的 `daily_kline` 版本,缺失记 `version:null`;用了池的日子记 `limit_up_pool` 版本)、`window.fixed_days`(精确的被访问日列表)。版本 = 内容 sha + inputs,同输入重跑同版本。

## 6 触发与回补

* registry `derive` 块(`algorithm ladder_v1`、`min_upstream_status PROVISIONAL`、`window_days 20`、rung 顺序、Kaipan/问财节流);`reconcile_plan` 的派生分支对最近候选日逐个调用 `derived_needs_build`:当日日K >= PROVISIONAL 存在 -> 无分区 `missing`;有分区但 `algorithm_version != ladder_v1`(旧接力分区)-> `algorithm_replaced`;记录的输入版本变化(含"当时缺失的日K/池现在到了")-> `inputs_changed`。
* 旧的 10-19..10-23、10-26(无)、10-27 分区**不手写**:新版本由 guardian 的回补机制产出;旧 `core_price_close_proxy` 版本在 `select_best` 中排名低于一切(`version_rank_key`),`latest` 规则允许同日新版本替换旧 latest(`storage._can_advance_latest`)。
* `ops.backfill_ladder_daily` 与盘后 `run_daily_phase` 调用同一实现(接力版已删除)。

## 7 TD 列映射(冷数据)

数据库同 C567(registry `daily_kline.td_sink.database`,`CORE_TD_DATABASE` 可覆盖)。超级表 `ladder_daily_v1`(子表 `l_<symbol>`),版本表 `ladder_daily_versions`(列同 `daily_kline_versions`,去掉 `needs_upgrade`)。

| TD 列 | catalog 行键 | 类型 |
|---|---|---|
| `ts` | 分区 `trade_date` 当日 00:00 +08:00(微秒) | TIMESTAMP |
| tag `symbol` | `symbol` | VARCHAR(8) |
| `limit_up` | `limit_up` | BOOL |
| `streak_n` | `streak_n` | INT |
| `streak_start_date` | `streak_start_date` | VARCHAR(10) |
| `day_source_chain` | `day_source_chain` | VARCHAR(600) |
| `quality` | `quality` | VARCHAR(64) |
| `version` | manifest `version` | VARCHAR(24) |

同步规则同 C567:取最佳 catalog 版本(旧代理版本忽略);TD 已有更高秩 -> `skipped_lower`;同版本同行数 -> `unchanged`;否则整日 DELETE+INSERT、读回核对(行数、`sum/max(streak_n)`、样本逐列、版本),通过后才写版本行;TD 不可用 -> `TdUnavailable`(30,可重试),核对失败 -> `TdVerifyError`(40)。入口:构建后的尽力同步(`build_ladder_daily.py`,失败只告警)与 `td_sync_daily_kline` 事件(同一次 sweep 后同步最近 N 个 ladder 日)。
Redis:不设热视图。判据:结果每交易日盘后产出一次,盘中没有读取者(连板梯队现由 `limit_up_pool.lb_days` 提供,见 C574);出现热消费方时再加 `core:ladder_daily:latest:v1`(hash symbol->JSON,TTL 36h,由 TD sink 在核对通过后写)。

## 8 已知限制

* 实时(盘中)不产出:`earliest_available_local` 15:00。
* Kaipan 池的口径与日K推算不同(见各日 `cross_validation` 与 `fw/reports/C577.md` 的差异分类:炸板回封收盘仍在涨停价、北交所新码不在日K universe、池未收录等)。
* 问财 rung 本卡未做真实调用(限流被另一进程占用),仅以记录型替身验证阶梯逻辑,见报告 OPEN。
* 稀疏行:不含 `limit_up=false` 的票(旧版含全 universe 5224 行);消费方若需全量需自行与 daily_kline 联接。
