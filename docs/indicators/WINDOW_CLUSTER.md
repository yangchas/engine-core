# 窗口统计簇细则

PLAN: D8（PLAN-MASTER §11.2，GOAL-datalayer G5）。状态：设计提案；新增表、入口、调度及协议均未实现，DDL 须单独审批。依据 `docs/INDICATOR_FRAMEWORK.md`、`/home/exedev/fw/design/32`、C777、C784；实施前遵守 fixture 和 evidence 规则。

## §1 范围与成员

F1 是新增唯一事实获取方，逐已收盘交易日取得每股 09:25 竞价匹配金额 `auction_amount` 和 09:45 累计成交额 `slot0945_amount`，分别发布 L0。F2a `auction_baseline` 计算市场及个股竞价统计；F2b `plate_amount_baseline` 计算板块两个时点的统计；F2c 个股竞价统计已包含在 F2a 的 `symbol_summaries`，不再另建获取或计算函数。三者共享历史事实但 F2a 不依赖板块关系，F2b 两时点分别判质量。

这是固定交易日滚动窗口：新日进入、旧日退出；中位数和分位数不能由一个累计总量精确更新，必须保留窗口内逐日值并排序重算。F1 增量采集不等于 F2 增量累计。缺失不等于零，历史口径不可用当前板块代替。

## §2 F1 获取与存储

按交易日历检查每个事实的完整日标记，补缺日默认每次最多 5 个交易日，优先最近日，再按日期降序；同一日两时点分别检查和提交。只有缺失或经证实修订才查询该时点；已完整的日与时点不再读源。首次回填最终应覆盖目标窗口；最新优先是获取优先级，F2 按时间升序排列样本。每个日、时点持租约 `core:lease:fact:{fact}:{date}`（300 秒，token 比较续租和释放），取得后复查标记；丢租不发布。中途 `0x073a` 立即停止，不 sleep、不在函数内拆分重试；已提交日保留，下次从缺标记处续跑。调度器按 §6 退避。这里的 5 日是待实测的负载上限，不承诺成功。

对单日分别一次查询，参数绑定或严格校验日期；不跨整窗口扫描，不做全表 GROUP BY：
```sql
SELECT ts, symbol, trade_date, auction_tag, match_amt_yuan
FROM market_data1.auction_snapshot_v2
WHERE trade_date = 'YYYYMMDD' AND auction_tag = '0925';
SELECT _wstart, symbol, LAST(amt_yuan)
FROM market_data1.stock_tick_v2
WHERE ts >= 'YYYY-MM-DD 09:30:00' AND ts <= 'YYYY-MM-DD 09:45:00'
PARTITION BY symbol INTERVAL(15m, 1s);
```
第一条在客户端按日、股选最大源 `ts` 的终值；第二条只接受 `_wstart` 为 09:30:01 的桶，取 [09:30:01,09:45:01) 中最后一个累计值，不把竞价额加进去。保留实际选中的源时间；现有 `LAST(amt_yuan)` 未返回源时间，须验证同一行配对方案及并列时间规则（UNKNOWN-W1），不能用交易日零点冒充源时间。C740 的切片 1/2/3/5 日分别返回 8576/32903/140363/271549 行，耗时 0.313/0.668/6.036/14.414 秒，随后连单日分区亦失败；不是稳定性能保证。C756 是日K缺口研究，不能作为本查询耗时证据。C731 的 146247 竞价行及 5796 行 09:45 片段来自不完整冷读，不能以片段行数推定单日完整。

每时点独立去重后有效非空、合法金额的不同 symbol ≥4000 才可发布该时点完整日标记；保留缺股及有证据的停牌、退市、未上市、源空白名单与原因，未知缺口不补零。仅达到 4000 不是板块每成分都有值的保证。09:45 不足阈值或查询不完整时不写完整标记，记录缺失日；即使竞价已完整也不得把 09:45 标完整。F2 缺该日正式依赖返回 30，诊断性统计若单独生成必须标 PARTIAL，不冒充正式产物。C731 的 5796 行是跨片段、不完整的样本，不能按 5796≥4000 推断完整。

拟议冷存逻辑 DDL（C784 DESCRIBE 两个源金额均 BIGINT；DECISION-6 原值原类型，非浮点；ts 为上海交易日零点）:
```sql
CREATE STABLE core_data.auction_amount_v1
 (ts TIMESTAMP, match_amt_yuan BIGINT, source_ts TIMESTAMP, version VARCHAR(24))
 TAGS (symbol VARCHAR(20), version_tag VARCHAR(24));
CREATE STABLE core_data.slot0945_amount_v1
 (ts TIMESTAMP, amt_yuan BIGINT, source_ts TIMESTAMP, version VARCHAR(24))
 TAGS (symbol VARCHAR(20), version_tag VARCHAR(24));
CREATE TABLE core_data.auction_amount_days_v1
 (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), missing_whitelist NCHAR(16384));
CREATE TABLE core_data.slot0945_amount_days_v1
 (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), missing_whitelist NCHAR(16384));
```
这只是列合同：同日多个可见版本的子表、日标记物理隔离和原子发布仍为框架 UNKNOWN-U2，以上普通日标记表同 ts 会覆盖，不能直接上线；必须先解决隔离、长度及版本可见性，数据写完、计数及摘要校验后最后发布标记。修订用新版本完整快照、校验后切换，旧版保留供 as-of 读取，不原位替换。C778 使用共表 `auction_amount_facts_v1` 的 symbol/slot tag 和单日标记表，`ensure_schema/complete_days/write_day/read_days` 分别建表、按 slot 的 ≥4000 标记选日、验证后写、严格读取；其先删除标记及逐 symbol 删除旧行再写入，缺版本隔离和血缘。逐股删除真实成本未测，失败可留下缺标记和部分数据；不得用于已可见版本替换。共表与分表只能选一个权威，不能双写成两个真相源；迁移结论见 §8。

事实消费者是 L1 基线计算，不要求低延迟逐股读，F1 无 Redis 热视图；Redis 丢失不影响事实完整性。L1 现行热视图继续服务读取方，目标存储见 §3。

## §3 F2 基线计算

目标日 D 的窗口由交易日历取严格早于 D 最近 64 个交易日，不以源中出现的日期反推窗口。F2a 读取该窗口 `auction_amount`；F2b 再读 `slot0945_amount` 与每个历史日 as-of 可见的该日 `plate_members` 快照及版本。每个时点的日完整标记、行、版本及可见时间均检查；不得用今日成员回填过去。日内每板块、时点把当日去重成员金额求和，成员为空或任一成员缺金额则该板块该时点该日缺席；不以 0 代替。F2a 市场按该日正式事实的金额求和，个股按各日有效金额统计；个股缺日记入该股 `missing_dates`，禁止把全市场达标当作该股全窗口齐备。

对排序样本 x[0..n-1]，p 分位令 h=(n-1)p、i=floor(h)、j=ceil(h)，结果为 x[i]+(x[j]-x[i])*(h-i)；n=0 为 NULL，中位数即 p=0.5。F2a 输出 p10/p25/median/p75/p90、最小最大、样本数、窗口和缺日，保留 `rows/market_summary/symbol_summaries` 与 `observed_source_dates` 的已修语义：观察到的市场样本日数而非候选日数。F2b 每板块每时点保留 `median_yuan/p25_yuan/p75_yuan/sample_days/amounts_yuan/dates_used/missing_dates/membership_versions`；日序和金额数组位置对应。统计为 DOUBLE，事实仍为 BIGINT。任一所需日缺失或样本不足 64 时 `warm=false`、PARTIAL；空样本不产生数值。已有 F2a 总体 READY 只看市场，F2b 对候选窗口短于 64 也可能 READY，均须补足逐实体 warm 语义。

存储决定：现行两个 L1 保留 catalog 不可变版本及 Redis 热视图作为迁移期间权威和消费兼容；按 `/home/exedev/fw/design/32` §3 B/C 最终迁至 TD `td_snapshot` 版本化快照 + Redis view，不能把 catalog 作为长期终态。框架 §7 的 L1 TD 方向与此一致；不新造另一张 TD 指标超级表。迁移前冻结快照 schema、版本和元数据（UNKNOWN-W2），双写对账并切换 reader；当前字段沿用现有产物，新增 `version/algo_version/input_refs/available_at/quality/warm` 及参数哈希，标记为提案，不伪称已有 DDL。

`input_refs` 示例仅展示两日，实际须列全 64 日和对应事实版本、available_at、成员版本及摘要，按 dataset/date 排序；不截断，超限改存可校验引用：
```json
[{"dataset":"auction_amount","trade_date":"2026-11-04","version":"v1","available_at":"2026-11-04T21:00:00+08:00"},{"dataset":"slot0945_amount","trade_date":"2026-11-04","version":"v1","available_at":"2026-11-04T21:00:00+08:00"},{"dataset":"plate_members","trade_date":"2026-11-04","version":"v2","sha256":"示意摘要","available_at":"2026-11-04T20:00:00+08:00"}]
```

## §4 依赖与单独运行

拟新增 registry 声明；F2a 只依赖自身实际所需的 0925，F2b 依赖三者：
```json
{"id":"auction_amount_facts","requires":[],"produces":["auction_amount","slot0945_amount"]}
{"id":"auction_baseline","requires":[{"dataset":"auction_amount","window_days":64,"min_quality":"FORMAL"}],"produces":["auction_baseline"]}
{"id":"plate_amount_baseline","requires":[{"dataset":"auction_amount","window_days":64,"min_quality":"FORMAL"},{"dataset":"slot0945_amount","window_days":64,"min_quality":"FORMAL"},{"dataset":"plate_members","window_days":64,"min_quality":"FORMAL"}],"produces":["plate_amount_baseline"]}
```
F1 无上游数据集依赖，只需交易日历及 market_data1 对应已收盘日可用。F2 默认只检查并读取 core_data，不现场补事实；显式 `--pull-upstream` 才在租约下调用 F1 补缺并重新检查，成员缺失由其独立获取方处理。仍缺则返回：
```json
{"contract":1,"status":"failed","error_code":"NOT_YET_AVAILABLE","retryable":true,"artifacts":[],"detail":{"missing":[{"dataset":"slot0945_amount","trade_date":"2026-11-04"},{"dataset":"plate_members","trade_date":"2026-11-05"}]}}
```

| exit | 提议合同 | 与现状区别 |
|---|---|---|
| 0 | ok，正式成功 | C773 现行 READY、PARTIAL 和复用均可能 0 |
| 10 | degraded / STALE_VERSION，旧合格版本，PARTIAL | 新增 |
| 20 | not_applicable / ALREADY_SATISFIED，同输入、算法、参数且已完整提交 | 通用码已登记；现行 plate 复用为 0 |
| 30 | failed / NOT_YET_AVAILABLE，可重试，detail 列全部缺项 | C773 对终态 0x073a 使用 30；新错误码须登记 |
| 40 | failed / CONTRACT_ERROR，确定性错误 | 现行确定性异常可 40 |

新增码和 status 须同步 error_codes 与运行时解码矩阵；`--force` 不绕过依赖或租约，`--dry-run` 不写不拉源。以上为目标合同，不是声称 C773 已实现所有映射。

## §5 失效与回填

已收盘事实版本不可变；上游修订如何发现、频率和检测窗口 UNKNOWN-W3（runner 查）。经证实修订由 F1 发布新版本并使所有包含该日的 F2 窗口失效；某日 `plate_members` 版本变化只重算包含该日的 F2b 窗口。算法、参数变化重算受影响版本；仅输入引用、算法、参数一致且完整提交可跳过，保留旧版本供 as-of 查询。

每日已有最新历史日时，首次补其余 63 日最多 126 次单日上游查询（每时点各一次；若已有完整标记则减少），另加已有日原本的两次；按每次最多 5 日、至少 13 批，失败额外计。不能把 C740 的单次 0.313 秒乘次数当完成时间：它只测 09:45 单次，竞价查询、写入、租约及失败耗时未知，完整回填耗时 UNKNOWN-W4（runner 查）。C784 的 market_data1 KEEP 60 日、core_data KEEP 3650 日均为自然日保留配置，不能等同 60 个交易日；窗口第 61–64 个交易日的源数据必然超出 60 自然日，实际不可得日可能更多。无法回填的日期明确列缺失，warm=false/PARTIAL；不能凭临近日期造数，持续日更积累 64 个完整历史交易日后才 warm。已有可信历史事实版本可复用，但不得把历史 oracle 当运行时输入。

隔离根或新环境先检查持久 core_data 的日标记并复用；没有事实则走限额 F1 补缺，F2 返回缺依赖，不触发整窗口 market_data1 回读。若使用现有 L1 降级须满足 §6 的 as-of 和质量限制。

## §6 编排与降级

拟议 F1 在交易日 20:00 夜间启动，避开 09:00–09:46、15:00–15:40、17:30–18:30；单次上限 5 缺日，串行逐日逐时点，有限次数、退避由调度器管理。F2b 现有 `plate_amount_baseline_0815` 08:15–08:25、policy_12 一次、540s（C774；现有 fetch 超时 300s）；保留该独立事件但改为只读 L0。F2a 现有 startup/prep 中构建（C747），建议移到事实就绪后的独立盘前事件，并让 prep 只检查结果，不重复抓源；具体事件 schema 和跨日 data_date_offset 须按实施卡验证。拟议 registry 见 §4，timeline 示意如下，不宣称已实现：
```json
{"functions":{"auction_amount_facts_close":{"handler":"fetch_item","args":{"item_id":"auction_amount_facts","max_days":5},"calendar":"trading_day","policy":"window_facts"},"auction_baseline_preopen":{"handler":"fetch_item","args":{"item_id":"auction_baseline"},"calendar":"trading_day","policy":"window_baseline"}},"timeline":[{"at":"20:00:00","run":[{"id":"auction_amount_facts_2000","fn":"auction_amount_facts_close","deadline":"23:00:00"}]},{"at":"08:00:00","run":[{"id":"auction_baseline_0800","fn":"auction_baseline_preopen","deadline":"08:10:00"}]}],"policies":{"window_facts":{"ladder":[{"name":"td_daily","attempts":3,"backoff":"10m","timeout":"540s"}]},"window_baseline":{"ladder":[{"name":"local","attempts":1,"timeout":"540s"}]}}}
```
调度策略重试不是 F1 内 sleep 或拆分；禁止跨禁用时段 catchup。F2 失败可选 `available_at <= as-of` 且产物交易日不晚于目标日的最新合格完整版本，以 `DATE_FALLBACK`、PARTIAL、来源日期和版本暴露；无合格版本即缺失。`slices/__init__.py` 已有日期回退及 PARTIAL 标记，但 C774 指出最终失败后自动降级尚未落实，事件动作与 schema UNKNOWN-W5（runner 查），不得声称已接通。

## §7 验证与验收

实施卡先写旧行为会失败的最小测试：
1. 两时点独立阈值和标记、缺金额不为零；不完整查询即使返回 5796 行也不能发布完整；同日第二次不访问上游。
2. 实际 64 个交易日和短窗口的日历边界、各板块当日成员缺股、线性分位数、个股与市场各自缺日、warm 和 observed_source_dates；默认缺依赖返回 30 和完整日期清单，显式 pull 才触发 F1。
3. 修订与成员版本失效、as-of 不看未来、幂等及旧版保留；两个时点单独失败；第 N 日模拟 0x073a 后已完成日保留，重跑只请求缺日，函数不 sleep。
4. 隔离冷启动用查询计数断言 F2 的 market_data1 查询数为 0；仅显式 F1 补缺按每缺日每时点计数，禁止整个窗口回读。

C731 `/home/exedev/validation/realdata/plate_amount_baseline_20261106/` 的竞价 146247 行及不完整 09:45 片段作为真实来源与失败覆盖证据；`tests/fixtures/real/auction_baseline_20261106` 为现有竞价夹具。实施时必须核对是否由 `examples/capture_real_fixture.py` 抓取原始响应并登记 `PROVENANCE.json`，不足则由 runner 依规范重新采集登记，不手写假行。runner 在最短可用真实窗口固定相同交易日、成员版本、源时间和 as-of，分别从直接 market_data1 读取及 F1 冷存读取，逐项比较日股值、板块日和、各分位、缺日和血缘；两时点均完整才声称一致，不将 C731 不完整片段当完整对照。记录原始输出和查询计数，不把测试替身当真实覆盖。

## §8 迁移

建议泛化 C778 设计而非直接合并：保留其分时点读取、整数校验和最后标记的接口意图，重做不可覆盖版本隔离、完整性及血缘，确定共表或逻辑分表唯一权威；旧代码逐股删除成本及失败恢复须真实验证。C778 未接真实 TD，不视作现行能力，不合并该分支。

顺序：① 冻结事实 schema、隔离和采集契约，先录真实夹具；② 实施 F1 并单日对账及断点恢复；③ 有限回填并核验标记和版本；④ F2 双读同一真实短窗口逐项对账，保留旧读取路径作回滚；⑤ 切换 `auction_baseline`、`plate_amount_baseline` 和 prep、registry/timeline，再切换 `slices/__init__.py` 的版本读取与日期回退；⑥ 完成消费清单、真实验证及回滚期后移除旧的 market_data1 直读、`core:plate_amount_obs:v1:` 易失观察缓存、prep 重复读取和 F2 内 `td_split_retry_depth`。移除前验证读者与回退兼容；F1 不以旧拆分重试代替调度退避。L1 catalog 到 TD snapshot 另按 design/32 双写、对账、reader 切换和退役流程执行。

## §9 与框架差异、缺口及自检

框架 §2 的事实热键未定，本细则明确 F1 不需要；框架列出两张逻辑事实表，C778 共表仍须选定唯一物理权威。框架 U2 版本隔离、U8 源时间、U9 成员来源、U13 最终降级均未解决；本 DDL 是列合同而非已通过审批的物理实现。框架 §6 的最早可用历史顺序适用于 F2 历史结果计算；本卡 F1 限额拉取优先最近日以尽早积累当前窗口，二者不同阶段。F2b 正式依赖不足返回 30，PARTIAL 诊断结果和旧版降级不是当日正式成功。

自检：F1 覆盖 §1–2、§4–8 的获取、存储、依赖、回填、调度、验证及迁移；F2 覆盖 §1、§3–8 的输入、计算、存储、独立运行、失效、调度、验证及迁移。C778 建议泛化、不直接合并，版本隔离完成后才切换。

## §10 UNKNOWN 与待 runner 查证项

- UNKNOWN-W1：09:45 LAST 值与真实源时间的同一行配对、终值并列规则，runner 用真实响应核验。
- UNKNOWN-W2：TD snapshot 的具体 schema、版本隔离、标记原子性及与框架 U2 的落地，TD 负责人确认并申请 DDL 审批。
- UNKNOWN-W3：已收盘事实的源修订检测机制与频率，runner 查。
- UNKNOWN-W4：63 日完整回填实际耗时、日查询及逐股替换性能、0x073a 恢复，runner 在允许环境测。
- UNKNOWN-W5：最终失败自动降级动作、跨日调度 schema 与现有 decoder 支持，runner 查。
- UNKNOWN-W6：历史逐日 plate_members 来源、覆盖、PIT 可见时间及缺口证明，数据负责人查（框架 U9）。
- UNKNOWN-W7：现有两组 fixture 是否满足原始采集入口及中央 provenance 登记，测试负责人查。
