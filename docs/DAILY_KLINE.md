# Daily K-line

## 现状

### 写入路径

| commit 路径 | `source` 标签 | 权威 `provider` 与尝试记录 | 状态与时间 |
|---|---|---|---|
| `data_pipeline/providers/daily_kline_chain.py:147`（Q2 快照） | 保留 `legacy_q2_final` | `q2_final`；一次带 `ACCEPTED`、尝试时间、耗时和原因的尝试 | writer 按活跃 universe 重算覆盖率与字段完整度，分类为 `PROVISIONAL`；新时间统一为上海时区 |
| `data_pipeline/providers/daily_kline_chain.py:497`（provider chain） | 保留各 provider 原标签 | 实际胜出 provider；所有尝试按顺序记录。Baostock 另记录 `login_ms`、`query_ms`、调用结果和错误码 | 业务状态由共享分类器决定；Baostock 调用向 `job_log/<日期>.jsonl` 追加 |
| `daily_kline_degraded.py:266` | 保留组合来源目录标签 | 按实际填充字段来源写 `q2_final`/`wencai`；来源无法唯一判定时写 `UNKNOWN`；记录各来源尝试 | 分类器决定业务状态；时间统一为 `+08:00` |
| `data_pipeline/jobs.py` 的 `JobRunner.run` | 保留 job source | 使用 fetch metadata 的 provider/attempts；仅有 Baostock 计时时写计时项；缺少尝试信息时标为 `UNKNOWN`，不伪造尝试时间 | 所有 commit 经 `ArtifactCatalog.commit` 重算质量、分类状态并规范时间 |
| `daily_history.py:512`（历史回填） | `baostock` | 从精确 source 得到 `baostock`；上游没有尝试元数据时记录 `UNKNOWN` 尝试，时间和耗时为空 | 同一 writer 重算质量与业务状态；该旧回填调用未提供逐调用计时 |

上述 catalog commit 共用 `data_pipeline/storage.py:_prepare_daily_kline_manifest`：覆盖率分母取同日 universe 中 `trade_status != "0"` 的股票；缺 universe 时写 `coverage=null`、`expected_basis=universe_unavailable`。它保存 `suspended_in_universe`、`suspended_without_bar` 与八个价量字段完整度，再调用 `classify_daily_kline`。`available_at`、`fetched_at`、`as_of` 和 `provider_attempts[*].attempted_at` 的可解析值统一转成带 `+08:00` 的 ISO 字符串；历史 manifest 不回写。

`examples/capture_daily_kline.py` 写独立 capture manifest，不调用 catalog commit；BaostockClient 在该路径也保存 login/query 计时、结果、错误码和尝试记录。

C541: `DailyKlineProviderChain.fetch_chain` 和 `run_data_jobs.py --job daily_kline` 使用整链墙钟预算，默认读取 `CORE_DAILY_KLINE_BUDGET_SECONDS`，未设置时为 240 秒；`--baostock-budget-fraction` 默认 0.6，显式 `--baostock-total-timeout-seconds` 继续与这部分预算取较小值。Baostock 到预算上限时保存 checkpoint 并返回已完成股票数，随后链路在整链剩余时间内尝试 Wencai、q2_final；下一次同日运行从 checkpoint 续取。非 Baostock 版本的 `provider` 记录实际来源，业务状态为 `PROVISIONAL`。Baostock 尝试记录 `result`、完成/预期股票数和 `checkpoint_saved`；catalog writer 保留这些字段。CLI 降级成功返回 job_contract 退出码 10，全部来源失败返回 30。链路验收和最终 manifest 的覆盖率都以同日活跃 universe 为分母。

`_supersede_prior`（`data_pipeline/providers/daily_kline_chain.py:394-399`）读取既有 `status == "SUPERSEDED"` 以跳过已替代版本；对其他旧版本写入 `status`、`superseded_by` 和 reconciliation 并原子替换旧 manifest。本卡不改其行为。

C554: guardian 按“分区 + 自然日 + heavy 静默 phase”累计日K补取预算，默认每窗口 3 次、两次真实取数至少间隔 10 分钟；窗口耗尽记录 `GAP_WINDOW_EXHAUSTED` 和 `next_retry_window`，之后由 `boot_reconcile` / `runtime_reconcile_*` 在下一静默窗口重新扫描。启动与运行时都检查最近 5 个交易日（旧到新，每轮只选一个缺失或降级分区）；盘中只排队到下一个静默 phase。连续 3 个交易日的窗口都耗尽时将 GAP 提升为 `GAP_ESCALATED` 并复用现有 alert handler 告警一次，后续窗口仍继续重试；正式版本写入后 GAP 标为 `RESOLVED`。参数可用 `CORE_DAILY_KLINE_UPGRADE_MAX_ATTEMPTS`、`CORE_DAILY_KLINE_UPGRADE_MIN_INTERVAL_SECONDS`、`CORE_DAILY_KLINE_UPGRADE_ESCALATE_TRADING_DAYS`、`CORE_DAILY_KLINE_BACKFILL_DAYS` 覆盖。

### Provider 阶梯与字段口径（C563）

Provider 优先级为 Baostock → 问财 → q2_final。`TIER_NO_DATA` 立即尝试下一档；限流、超时和网络错误可重试并继续阶梯；参数或代码错误确定性失败，不继续调用低优先级来源。仅配置 `wencai` 时只调用问财。低档结果标记 `degraded=true` 和 tier，不覆盖同日已有的更高质量版本。问财日期只从简洁日期加字段列表查询，例如 `2026-10-26开盘价，最高价，最低价，收盘价，前收盘价，涨跌幅，成交量(股)，成交额(元)，股票代码`；不要加“A股日线行情”等市场描述。验收人先前实验中，`2026年10月26日A股日线行情...` 的字段日期后缀落在 `20261027`；简洁的 `2026-10-26开盘价，收盘价，涨跌幅` 与 `20261026收盘价，涨跌幅，前收盘价` 返回 `[20261026]`。本卡实抓的简洁完整字段查询也返回 2026-10-26 后缀，响应为 30 行；列日期由适配器记录到 `source_dates`，一致性标记随 manifest 保存，日期拒绝逻辑留给 C564。

规范行（C579 改定，取代 C563 的基点方案）是“所有现有真实分区列集 ∪ 消费方实际使用列”的兼容超集：`symbol`（六位数字，必有）、`code`（`sh.`/`sz.`/`bj.` 前缀，与 2026-10-19 起的线上分区一致，可空）、`date`、价格元、**`pct_chg` 百分数 float（1.5044 即 +1.5044%，与线上所有分区一致，四位小数）**、`turnover_pct` 百分数（可空，问财/q2_final 无此数据时写 null，不写 0）、成交额元、成交量股（float）、`suspended`（bool；来源不提供则 null，不推断）、`source`/`degraded`/`tier`。来源转换依据：

| 来源 | 价格 | `pct_chg` | `turnover_pct` | 成交额 | 成交量 | 证据/转换 |
|---|---|---|---|---|---|---|
| Baostock | 元 | 百分数，原样 | 百分数，原样 | 元 | 股 | 线上 2026-10-27 `baostock>wencai>q2_final` 目录 5224 行；新链同日产出逐列 0 不一致 |
| 问财 | 元 | 百分数，原样 | null（查询不含） | 元 | 股 | 真实响应请求明确 `成交量(股)`、`成交额(元)`；600519 的成交量 4,159,916、成交额 5,574,287,630 元 |
| q2_final | 元 | `px/pc-1` 的百分数 | null（Redis 无） | 元 | 手 → 股（×100） | 线上 2026-10-26 `legacy_q2_final` 的 volume 为手（manifest 写 UNKNOWN），C579 实测手→股后与 Baostock 逐只一致；q2 的 `mx/mn` 不等于 Baostock 的当日高低（见下） |

`pct_chg` 为基点的方案已作废：下表证明读取方全部按百分数使用，基点会让它们静默算错。`registry/daily_kline.py` 里的 `bp` 只是闸门内部的换算目标（`registry/convert.py` 把来源的百分数乘 100 做校验），不是落盘单位。TD 的 `daily_bar_v1.pct_bp` 列映射由 TD sink 负责从百分数换算，落盘分区一律是百分数。

### 消费方取证（C579）

grep 范围：本仓库 src/examples/tests，以及线上旧系统 `/home/exedev/services/engine-next/current` 与 `releases/*`（只读）。

| 位置 | 读取的列 | 假定单位 | 可空性处理 |
|---|---|---|---|
| `daily_history.py` `calculate_daily_indicators` | `close`、`turnover_pct`（回退 `turn`）、`pct_chg`、`date`、`symbol` | `pct_chg` 百分数（阈值 9.8/19.8/29.8 即涨停百分点）；`turnover_pct` 百分数 | null 被跳过，窗口内有 null 则 `limit_up_days_5` 为 null |
| `daily_history.py` `core_history_rows`/`build_daily_report_features` | 全列透传，另读 `amount_yuan`（回退 `amount`）、`date` | 元 | null 跳过 |
| `plate_peer_view.py` `limit_streaks` 与池统计 | `pct_chg` | 百分数（阈值 9/19/29） | null 中断连板，池均值跳过 null |
| `daily_pipeline.py:881-945`（收盘复盘日线行） | `close/pre_close/open/high/low/pct_chg(→close_pct)/amount_yuan/volume(→volume_shares)/suspended` | 元；百分数；股 | 缺失保持 None；`high/low` 用 `pre_close` 算百分点 |
| `daily_pipeline.py:1008-1030`（TD 旧表 `daily_kline`） | `volume,amount,turnover,pct_chg` | 同上，百分数 | 不读分区，属 TD 路径 |
| `close_review.py:760-820,1038-1044` | `amount_yuan`（回退 `amount`）、`close`、`pre_close`、`pct_chg`（仅回退） | 元；百分数 | 缺 close/pre_close 才用 `pct_chg` |
| `extremes_review.py:387-408,489-494` | `pre_close`、`close`、`pct_chg`（回退） | 元；百分数 | 同上 |
| `stock_day_facts.py:262-277` | `open/high/low/close/pre_close`、`volume`（→`day_volume_shares`）、`amount_yuan`、`pct_chg`（→`day_pct`） | 元；股；百分数 | 缺 `pct_chg` 用 close/pre_close 算 |
| `data_pipeline/ops.py:33`、`:312-330`（`ladder_daily`） | `close`、`pre_close`、`pct_chg`、`amount_yuan`（回退 `amount`）、`volume`、`code`、`universe_version` | `Decimal(close)/(1+Decimal(pct)/100)` 即百分数；元；股 | `amount==0` 判无成交；`pct` 为 None 不判涨停 |
| `live_session.py:203-216`、`slice_cache.py:24-36` | `open/high/low/close/pct_chg/amount_yuan` | 元；百分数 | null 保留 |
| `registry/daily_kline.py`（闸门） | 全部价量列；`pct_chg` 经 `percent→bp` 内部换算 | 来源百分数 | 按 field_completeness 统计 |
| `guardian/gates.py`、`registry.py`、`commands.py`、`lineage.py`、`access.py` | 只读 manifest（coverage/business_status/row_count/provider） | 不读行内单位 | — |
| `limit_up_pool_derivation.py:100,259` | 元组第 9 列 `pct`（百分数，`/100` 换算） | 百分数 | 来自 TD 行元组，不读分区 |
| 线上旧系统 `engine_next`、`engine_v2`、`web`（current 与 releases/*） | 读 TDengine 表 `daily_kline`（`ts,open,high,low,close,volume,amount,turn,pct_chg`，`tdengine_service.py:113-337`）；`engine_next/contracts/schema_contracts.py` 记 `pct_chg/amount/preclose` | `pct_chg`、`turn` 百分数（Baostock 原值） | **未找到**读取 `~/core-data/daily_kline` 分区文件的代码 |
| 列 `turnover`、`mismatch`、`universe_version`、`available_at`、`open_missing`、`quality` | 除 `ops.py` 透传 `universe_version`/`code` 外**未找到**读取方 | — | 新链不产出；比较工具逐列列出这些“仅旧分区有”的列（不隐瞒） |

数据与取证结论：规范列集 = `symbol, code, date, open, high, low, close, pre_close, pct_chg, turnover_pct, amount_yuan, volume, suspended, source, degraded, tier`。线上 2026-10-27 分区另有 `turnover`（与 `turnover_pct` 数值相同）、`mismatch`、`universe_version`：无读取方，故新链不写（`ops.py` 对 `universe_version` 的透传只进 `ladder_daily` 的事实行，缺失为 None）。需要这三列的读取方出现时再按其实际用法加。

### 兼容性证明（C579，`examples/compare_daily_kline_partitions.py`）

比较工具按列检查：候选必须含参考的所有列（无读取方的列在 `columns_only_in_left_waived` 里列出）、候选 `field_units` 必须等于规范、列值类型一致、`pct_chg` 在每侧行内自测为百分数（`|pct-(close/pre_close-1)*100|<=0.02`）、成交量自测为股（`amount/volume` 落在 `[low,high]` 内；参考若是手则识别并 ×100 后比较）、`code` 为 `sh.|sz.|bj.`+6 位、`symbol` 为 6 位数字，再逐只逐列比值（价格 `max(0.011,0.2%)`、百分点 0.02）。证据在 `fw/evidence/C579/proof.json`，复现 `run_proof.py`。已知来源差异：q2_final 的 `mx/mn`（高/低）不是 Baostock 的当日高低（2026-10-23 的 59 只中高 52 只、低 18 只超容差），`close/pre_close/pct_chg/amount_yuan/volume` 全部 0 不一致；该差异是来源语义，不是单位问题，比较工具用 `--skip-fields high,low` 显式跳过并记录。

2026-10-26 真实 Baostock 与问财共同覆盖的 29 个活跃 universe 代码，`open/high/low/close/pct_chg` 比较分别为 29 项、0 不一致；比较容差为 `max(0.011, 0.2%)`。本次问财响应只覆盖该日活跃 universe 的 29/5,219（排除 1 条 universe 外记录），不能据此声称全市场覆盖。清洗时先丢弃并计数 universe 外记录；重复代码按规范非空字段最多、再按规范 JSON 字典序最小确定性选取，并记录重复总数、冲突代码数和样本质量说明。覆盖率分母固定为本地同日活跃股票 universe；停牌股票不进入分母。

### 读取方

- `data_pipeline/access.py:41-53,86-100,248-263`：读 manifest/catalog 的 `business_status` 与 `quality.coverage`，选择版本并标注非 FORMAL 结果。
- `data_pipeline/registry/gate.py:112-126`：用 `quality.coverage` 和 `quality.status` 生成数据 gate。
- `guardian/registry.py:22-28,55-64,115-118`、`guardian/commands.py:55-58`：读取 coverage、quality status、business status，判断待升级和后置条件。
- `guardian/gates.py:201-252`：根据 available_at、行数和 universe 行数计算 daily_kline ready/coverage。
- `data_pipeline/ops.py:148,441-471`、`data_pipeline/jobs.py:341`、`examples/run_data_jobs.py:63-65,421`：库存、缓存发布和 job 报告读取 business status、quality status 或 coverage。
- `daily_history.py:563-564`：重建历史前检查 coverage；`data_pipeline/lineage.py:13-18`：把 daily_kline 的 quality status/source 写入派生输入 lineage。
- `slices/__init__.py:602`：将 partial business status 传播为 slice 结果状态。

## 15:05 close capture event (C566)

`daily_kline_close_1505` (schedule.json, profile `daily_kline_close`) fetches today's close early and tags it provisional until the Baostock formal partition arrives (17:40+).

- Trigger 15:05:00 on trading days, cost `heavy` (the single heavy slot is shared with `baostock_probe_1730`, so the two never run together), `catchup_mode: run_in_window`, `deadline = late_until = 17:35:00`. 17:35 is deliberately earlier than the Baostock probe's `available_after` 17:40 so a late guardian start can never begin a fetch inside Baostock's window; an attempt already running at 17:35 is not killed (its own timeout, at most 10 minutes, still applies).
- Ladder (order, attempts, backoff, timeout are config only; Python never decides the order): rung 1 `wencai` (`args.providers = "wencai"`), rung 2 `q2_final` (`args.providers = "q2_final"`, Redis read-only). Each rung calls `guardian_handler.py run --handler daily_kline_close` once, which runs `run_data_jobs.py --job daily_kline --providers <rung provider> --write`. Swapping the two rungs in schedule.json makes `q2_final` run first.
- Rung semantics (`on_timeout`, `alert_on_fallback` tier fields, used only by this event): a rung timeout advances to the next rung without retrying the same rung; `TIER_NO_DATA`/`TIER_DATE_MISMATCH`/`TIER_DATE_NO_REFERENCE` advance silently; an exit-40 failure with any other code (reported as `DETERMINISTIC_FAILURE`) also advances and, if a later rung succeeds, sends exactly one `TIER_FALLBACK` alert naming the failed and the succeeded rung. Both rungs failing is `FAILED_FINAL` with one alert and nothing written.
- Catalog result: wencai writes a tier-1 version (`business_status=PROVISIONAL`, `status=DEGRADED:wencai`, or `SAME_DAY_TEMPORARY` on the trade day itself, which never moves `latest`); q2_final writes tier 2 `DEGRADED:q2_final`. Before fetching, the handler ranks the existing versions of the partition (baostock/FORMAL 0, wencai 1, q2_final 2; symbol-file samples ignored); an equal or better version answers `ALREADY_SATISFIED` (exit 20, ledger `SKIPPED_SATISFIED`), so a restart or a later trigger never refetches or lowers the partition. The formal Baostock version written after 17:40 supersedes both through the existing tier rules.
- Catch-up: a guardian that starts after 15:05 and before 17:35 runs the event immediately; after 17:35 it is `MISSED` (the formal Baostock events and `boot_reconcile` cover the day).
- `_daily_kline_candidates` reads the boundary time from the schedule event named by the registry `late_policy.trigger_event`; there is no hard-coded 17:30 fallback, and an unresolvable event raises.

## Manifest contract fields (C576)

All writers get `provider_chain`, `degraded`, `field_units`, attempt `row_count`, `quality.dropped_*` and
`source=<actual provider>` from one function; see docs/DATASET_CONTRACT_CHECK.md, section "daily_kline writer and
legacy partitions". Old partitions stay readable and unchanged.
