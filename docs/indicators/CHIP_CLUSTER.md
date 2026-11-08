# 筹码簇细则

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。状态：待实施设计合同；DDL、字段、入口和配置均为提案，不代表已上线。本文服从 `docs/INDICATOR_FRAMEWORK.md`（框架），时间 Asia/Shanghai。证据：C787 `/home/exedev/fw/reports/C787.md`，C788 `/home/exedev/fw/reports/C788.md` 和 `/home/exedev/validation/realdata/chip_dde_20261107/`，C786 `/home/exedev/fw/reports/C786.md`；旧算法见 `/home/exedev/services/engine-next/current/engine_next/adapters/runtime_chip_runner.py:88`，只读参考，不复制旧源码。

## §1 范围与成员

仅筹码峰，框架 DECISION-1 方案 A，按股日输出十列；属于 L1 派生窗口型，不把事实和策略结论混合。C788 对 10 股各 120 根、10 字段共 100 项，在不复权 close 和 turnover_pct 输入上以绝对误差 1e-9 对比旧函数全部通过；样本无空、零换手或停牌，不证明这些边界和生产复权语义。新生产口径是前复权，不可直接将该旧输出当生产期望。

## §2 输入与质量

读取已发布 L0 `daily_kline`（日期、symbol、close、turnover_pct，换手单位百分数）和 `adjust_factor` 完整快照，以 as-of 选可见版本；按日期升序每股取截至目标日最近最多 120 根非停牌 K 线，至少 5 根。停牌日不是 bar，与价量簇口径一致；缺日、停牌与确认非交易日不可混同。检查每个日期、实体、白名单、完整标记、版本和因子覆盖；因子须含窗口起点前最近事件。

C786 样本口径：每根找 event_date ≤ 当日的最近事件，Decimal 解析 `foreAdjustFactor`，前复权 close = 原 close × 该因子，事件日使用新因子；非事件日期不要求有因子行。因子 UNPROCESSED 时不计算、不产出且保留状态；NO_EVENTS 使用因子 1.0 并标记 PARTIAL（FACTOR_NO_EVENTS）；BEFORE_FIRST_EVENT 使用首个事件因子并标记 PARTIAL。无效因子不可计算；close 为 NULL/非有限时结果 NULL、quality=PARTIAL。换手 NULL 的窗口十列全 NULL、quality=PARTIAL，不跳过该行、不补 0；上游整个依赖未就绪时按 §7 返回 30。少于 5 根十列 NULL、warm=false、PARTIAL；5–119 根可算但 PARTIAL、warm=false，120 根且输入完整有效方 warm=true、可 FORMAL；`bars_used` 为窗口实际根数，不把缺失填成有效根。负换手视为非法输入，整窗口十列 NULL、PARTIAL，记录原因；不截到 0 或沿用旧负权重，避免制造负筹码分布。停牌行不计入窗口，跨停牌保留原窗口。

## §3 逐步算法与输出

每个窗口均独立计算，以下是规范伪代码；所有浮点按 IEEE 754 binary64，按给定顺序运算，最终按 Python `round` 语义取整，不做其他中间取整。旧函数可从异常返回空对象；新接口用 NULL 和质量标记表达。运行库的边界浮点差异用 C788 的 1e-9 比较已舍入输出，不得提前舍入价格或权重。

```text
rows = ascending_validly_ordered_rows_for_symbol_through(date).tail(120)
if len(rows)<5 or required_value_missing_nonfinite(rows) or any(turnover_pct<0):
    emit all ten NULL, PARTIAL, warm=false, bars_used=len(rows); stop
C = [float(adjusted_close(row)) for row in rows]; T = [float(turnover_pct) for row in rows]
lo=min(C); hi=max(C)
if lo == hi:
    emit peak_price=lo, concentration_70=0, dense_bins=1;
    emit other seven fields NULL; quality=PARTIAL; warm=false; stop
edges = linspace(lo, hi, 51); centers[i]=(edges[i]+edges[i+1])/2 for i=0..49
weights = 50 zeros
for price, turn in zip(C,T):
    t=min(turn/100,1); weights[:] *= (1-t)
    i=min(int((price-lo)/(hi-lo)*50),49); weights[i]+=t
peak = first index of maximum(weights); peak_price=round((edges[peak]+edges[peak+1])/2,2)
total=sum(weights)
if total <= 0:
    emit peak_price, peak_weight_pct=0, chip_percent=0, avg_cost=peak_price,
         profit_ratio=0, loss_ratio=0, cost_low_70=peak_price, cost_high_70=peak_price,
         concentration_70=1, dense_bins=count(weights[i]>total*0.05)
    quality=PARTIAL; warm=false; stop
peak_weight_pct=chip_percent=round(weights[peak]/total*100,2)
avg_cost=round(sum(centers[i]*weights[i])/total,2)
profit_ratio=round(sum(weights[i] for centers[i]<=C[-1])/total,4)
loss_ratio=round(max(0,1-profit_ratio),4)
price_cumulative=cumsum(weights in ascending price order)
cost_low_70=round(centers[min(first index with cumulative>=total*0.15,49)],2)
cost_high_70=round(centers[min(first index with cumulative>=total*0.85,49)],2)
ranked=sort(weights, descending); k=first index where cumsum(ranked)>=total*0.7
concentration_70=round((k+1)/50,4)
dense_bins=count(weights[i]>total*0.05)
emit all ten, bars_used=len(rows), quality and warm per §2
```

`linspace` 两端均包含；箱为 `[edge[i],edge[i+1])`，末箱包括最高价；内部边界归右箱，公式的浮点运算顺序为准。峰值并列选最低序号；获利包括中心恰等收盘价。`chip_percent` 是峰箱占总权重的百分数，与 `peak_weight_pct` 完全相同，不是获利比例。旧名映射：`peak_weight`→`peak_weight_pct`、`lower_cost`→`cost_low_70`、`upper_cost`→`cost_high_70`、`concentration`→`concentration_70`、`dense_area_count`→`dense_bins`；其余同名。价格两位、百分数两位、比例四位、箱数整数；同价峰价按旧函数原样 float，不另行四舍五入。全同价即使换手全零也先走同价分支；旧函数仅返回三个字段，其余未提供，新合同显式 NULL。非同价零总权重沿用旧数值，包括 loss_ratio=0，与正常分支公式不同；为防误认有效分布标 PARTIAL。旧代码无换手列或 NULL 时补 0，且对负换手无下限；新合同分别拒绝缺失及负值，是有意差异。

## §4 一次遍历与复杂度

按版本批量读取、排序及核实输入，每股每目标日只切一次窗口并一次生成复权价与换手数组；该数组同时用于求边界和随后按日更新 50 箱，所有输出共用该分布。不要为每个输出重新切窗口或重复计算分布。箱边界由窗口最高最低价决定，旧分布不能跨日接续；无需状态列，不保存分布向量。单目标日运算约 120×50×5514=33,084,000 次箱更新，另有每股 50 箱排序与 I/O；逐股流式内存约 O(120+50)。5514 为规模估算而非实时股票总数，耗时需实测。

## §5 存储

拟议 TD 冷层 `core_data`，3650 日；ts 是交易日上海零点，input_refs 为完整输入版本、日期范围、因子快照与参数哈希的 JSON，不能截断。DDL 须单独审批及验证；不是实际建表命令：

```sql
CREATE STABLE core_data.chip_indicators_v1 (
 ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), input_refs NCHAR(12000),
 available_at VARCHAR(40), quality VARCHAR(16), warm BOOL, bars_used INT,
 peak_price DOUBLE, peak_weight_pct DOUBLE, chip_percent DOUBLE, avg_cost DOUBLE,
 profit_ratio DOUBLE, loss_ratio DOUBLE, cost_low_70 DOUBLE, cost_high_70 DOUBLE,
 concentration_70 DOUBLE, dense_bins INT
) TAGS (symbol VARCHAR(8));
```

同 tag 同 ts 的 version 列不能保证物理多版本隔离；框架 UNKNOWN-U2 由 TD 负责人决定分表、隔离及原子发布，解决前不可生产部署。各日独立完整标记包含 trade_date、version、business_status、row_count、sha256、available_at、quality、missing_whitelist；数据校验后最后发布，available_at 是实际可见时刻。

Redis `core:ind:chip:2026-11-06` 为 hash，field `600519`，value 为 JSON；下方十个数字逐字取 C788 `expected.jsonl`，是**不复权输入旧函数输出**，不是前复权生产结果；元数据是演示占位而非历史事实：

```json
{"version":"example","algo_version":"example","input_refs":[{"dataset":"daily_kline","version":"example"},{"dataset":"adjust_factor","version":"example"}],"available_at":"2026-11-06T23:30:00+08:00","quality":"PARTIAL","warm":false,"bars_used":120,"peak_price":1308.75,"peak_weight_pct":7.42,"chip_percent":7.42,"avg_cost":1300.01,"profit_ratio":0.9822,"loss_ratio":0.0178,"cost_low_70":1234.02,"cost_high_70":1352.34,"concentration_70":0.34,"dense_bins":5}
```

缓存最近 5 个交易日，TTL 604800 秒。热层丢失按 as-of 从 TD 已完成且合格版本校验标记、行数、摘要、血缘和时间后重建 hash 并原子切换、设 TTL；无合格结果返回缺依赖，不回源。原子替换机制 UNKNOWN-C2（实施验证卡）。

## §6 增量、失效与回填

每个股日重新读取并计算完整窗口，增量只指新增日期的结果，不接续旧权重。新除权事件使该股全历史受影响筹码窗口失效，按新因子对各窗口整体重算；普通 D 日日K或换手修订重算该股所有包含 D 的窗口；算法版本、参数或复权口径改变全量重算并发新版本。其他股票和不含 D 的普通修订窗口不受影响。按日期升序回填，保留 as-of 可见旧版；相同完整输入引用、算法及参数和已完整提交时返回 20，缺血缘不可跳过。热键丢失只重建缓存。

## §7 函数契约与单独运行

拟议 `run(date, window=120, pull_upstream=False, force=False, dry_run=False, available_at=None)`；默认 as-of 时钟语义 UNKNOWN-C3（编排实施卡）。声明是拟新增 registry 字段，window_days 为交易日：

```json
{"id":"chip_indicators","requires":[{"dataset":"daily_kline","window_days":120,"min_quality":"FORMAL"},{"dataset":"adjust_factor","window_days":120,"min_quality":"FORMAL"}],"produces":["chip_indicators"]}
```

检查所有日期、实体、版本、完整标记和 as-of；因子为有覆盖证明的事件快照。默认缺依赖不拉源；显式 `--pull-upstream` 才对每个缺失事实日期持 `core:lease:fact:{fact}:{date}` 300 秒 token 租约调用唯一获取方，续租和释放比较 token，丢租停止发布，完成后重新检查完整标记。`--force` 不绕过依赖、租约；`--dry-run` 不写不拉。依赖已 FORMAL 但个股字段缺失按 §2 产出 PARTIAL，不能将 PARTIAL 宣称完整 FORMAL。

| exit | status / error_code | 含义 |
|---|---|---|
| 0 | ok / 空字符串 | 成功 |
| 10 | degraded / STALE_VERSION | 合格旧版本，非重试，需新增登记 |
| 20 | not_applicable / ALREADY_SATISFIED | 幂等跳过 |
| 30 | failed / NOT_YET_AVAILABLE | 依赖未就绪，可重试，需新增登记 |
| 40 | failed / CONTRACT_ERROR | 合同错误，不重试 |
| 64 | failed / CONFIG_ERROR | 配置错误，不重试 |
| 75 | failed / BAOSTOCK_CONNECTION_ERROR | 上游或执行暂时故障，可重试 |

新增错误码须同时登记 error_codes.json 和运行时解码矩阵。示例为格式，不声称该事实缺失：
```json
{"contract":1,"status":"failed","error_code":"NOT_YET_AVAILABLE","retryable":true,"detail":{"missing":[{"dataset":"daily_kline","trade_date":"2026-11-06","symbol":"600519"},{"dataset":"adjust_factor","trade_date":"2026-11-06","symbol":"600519","reason":"factor_coverage_unknown"}]}}
```
拟议 CLI，模块实现后才可运行：
```sh
python -m engine_core.chip_cluster --date 2026-11-06 --window 120 --available-at 2026-11-06T23:30:00+08:00
python -m engine_core.chip_cluster --date 2026-11-06 --window 120 --pull-upstream
```

## §8 编排

拟新增 timeline 完整片段，与价量簇同一 21:30 sweep/兜底事件，避开 17:30–18:30 Baostock 窗口。`after` 空不表示依赖已经满足；两个簇各自检查 L0，日K FORMAL 后 chain_sweep 触发是待实施接线，夜间事件兜底。`on_deadline=degrade` 不等于最终失败自动降级，框架 U13 未解决前不可假设。
```json
{"functions":{"chip_indicators":{"after":[],"args":{"item_id":"chip_indicators"},"available_after":null,"calendar":"trading_day","cost":"medium","data_date_offset":0,"depends_on":[],"enabled":true,"gate":"calendar_ready","handler":"fetch_item","late_until":"23:30:00","on_deadline":"degrade","on_phase_mismatch":"defer","order":null,"phases":["NIGHT"],"trigger_type":"window","policy":"policy_12"}},"timeline":[{"at":"21:30:00","run":[{"fn":"chip_indicators","id":"chip_indicators_2130","deadline":"23:30:00"}]}],"policies":{"policy_12":{"catchup":"run_in_window","retry_backoff":"","retry_until_late":false,"silent_until_last":false,"ladder":[{"name":"legacy","attempts":1,"backoff":"","backoff_cap":"5m","timeout":"540s","budget_enabled":false}]}}}
```
registry `items` 数组加入 §7 JSON；requires/produces 当前运行时不识别，须实现 schema、handler、sweep 和 native 校验，不可声称已有能力。

## §9 验证与验收

C881 真实 TD 复验：31 只股票重建耗时 9.2 秒、单日增量耗时 0.19 秒；按 5,565 只线性外推重建约 1,655 秒、单日约 27–36 秒，均为外推，待全市场实测。

1. 实施卡先检查 `examples/capture_real_fixture.py` 的捕获及登记接口；将 C788 `inputs.jsonl`、`expected.jsonl` 和 `PROVENANCE.json` 按捕获规范纳入 `tests/fixtures/real`、中央 `PROVENANCE.json`，保留来源、旧源码哈希、原始查询与不复权标识；若现有捕获器不支持此来源，先扩展捕获器并可追溯登记，不手造假上游响应。逐股将 turnover_pct 映射旧 turn，比较十字段和旧名映射，绝对容差 1e-9，100/100；只作为不复权兼容回归。
2. 使用真实因子快照对相同窗口逐日按 §2 前复权，以独立实现执行 §3 生成并登记新期望，记录输入/因子版本、算法、生成程序及哈希；交叉核验，明确标注为前复权新算法期望，绝非旧系统输出。
3. 从真实数据找缺 turnover 窗口，验证十列 NULL、PARTIAL、不补零；若找不到，以真实窗口中一项置 NULL 的合成变体验证缺失处理并显式标注。
4. 从真实数据找负换手、全同价、少于 5 根和零总权重；未找到则以真实窗口局部变换构造合成边界并注明原因，不冒充原始响应。分别验证拒绝负值、同价三值七 NULL、预热不足及旧零总量退化；另覆盖最高端点、并列峰、严格 5% 以及 15%/85% 边界。
5. 移除隔离测试依赖标记，断言返回 30 且 detail 含 dataset、日期、实体；默认无上游调用，显式拉取检查租约和重读。验证修订、除权、版本失效和 as-of。
6. 仅在隔离测试 `core:` 键清空热缓存，以真实 TD 合格结果重建，断言值、版本、TTL、完整标记及无上游调用；禁止删除 legacy 键。遵循 `docs/TESTING.md` 及 live 契约测试要求，记录实际命令与原始输出。

## §10 迁移映射

C591 筹码待办从旧 daily_chips 和 daily_factors 中的 `peak_price/peak_weight/upper_cost/lower_cost/avg_cost/profit_ratio/concentration` 并入本簇；MACD 等价量因子归价量簇，DDE 独立。现有 `docs/DAILY_INDICATORS.md` 的 `daily_indicators` 是不复权价量衍生，不含本簇；保持既有消费者，先补 L0 120 日和因子、再实现、双读对账、完成消费迁移及回滚窗口后才停旧链路。C591 原要求全市场 120 根后再执行；本合同允许 5–119 根只发布 PARTIAL，不能当正式 120 根结果。

旧 `chip_batch_runner` 的 web 读取方到新 L2 的具体路由、字段和切换条件 UNKNOWN-C4（runner 读取链追踪卡）；在查清前不得改写旧缓存或假设可直接切换。L2 应从按 as-of 合格的 L1 构造消费视图，映射 §3 旧名并显式标注前复权/质量，不能反写 L0 或冒充旧不复权值。

## §11 自检与旧行为差异

自检：§3 覆盖取窗、校验、同价、分箱、日衰减、零总量和全部十列公式、端点、并列与舍入；§5–§9 覆盖存储、失效、契约、调度及真实数据验证。旧行为差异：缺 turn 从补零改为十列 NULL/PARTIAL；负换手从允许负权重改为拒绝；同价未返回的七列明确 NULL/PARTIAL；非正总权重数值沿用旧值但质量降为 PARTIAL；生产输入从不复权转前复权；不足 120 根可算但 PARTIAL；缺失和异常用显式质量表达。C788 不覆盖这些边界。

## §12 与框架文档的差异/缺口

框架 DECISION-1 的同价、非正总量和负换手待细则补齐：本合同给出具体退化与负值拒绝；旧负值不设下限与新数据质量红线有差异，需实现卡验收。框架 §6 新除权要求该股全历史失效，本合同逐受影响窗口重算，不仅限最新窗口。C591 的满 120 根门槛与 DECISION-1 至少 5 根并存：5–119 根只为 PARTIAL，不改变 C591 的正式交付门槛。

- UNKNOWN-C1：停牌有效性，runner 真实源取证卡。
- UNKNOWN-C2：Redis 原子重建替换机制，实施与并发验证卡。
- UNKNOWN-C3：默认 as-of 时钟，编排实施卡。
- UNKNOWN-C4：旧 web 消费路由与 L2 衔接，runner 读取链追踪卡。
- 框架 UNKNOWN-U2：TD 多版本物理隔离和提交标记，TD 设计/实施卡；UNKNOWN-U13：最终失败降级 schema 和 native 支持，编排卡。
