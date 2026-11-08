# DDE 簇细则

PLAN: D8 数据层欠账（PLAN-MASTER §11.2；GOAL-datalayer G5）。本文是待实施设计合同；新增表、标记、命令、registry 字段和编排均非现状，时间为 Asia/Shanghai。依据：`docs/INDICATOR_FRAMEWORK.md` DECISION-2、§4–5、§9；C784/C787/C788 报告及 `/home/exedev/validation/realdata/indicator_samples_20261107/`、`/home/exedev/validation/realdata/chip_dde_20261107/`；现行 `src/engine_core/data_pipeline/daily_dde.py`、`src/engine_core/guardian/daily_dde_sync.py`、registry/timeline，C600/C615。

## §1 范围与成员

F1 `daily_dde` 是独立的开盘啦获取函数，输出 L0 `daily_dde_v1` 的每股每日 DDJE 原值，不依赖日K；F2 `dde_derived` 只读 L0 DDE 和 L0 日K成交额，输出 L1 `dde_indicators_v1.ddje_ratio`。二者可以分别执行和检查各自依赖，也可以在 F1 与日K就绪后由编排串起。`large_net/ddx/ddy/ddz` 无已证实来源，不复现、不补零；`ttag` 语义未知，不存。Fact 不等于策略结论。

## §2 F1 获取合同

调用 `getHisStockDDE(symbol, target_date)`，symbol 为六位股票代码、日期 `YYYY-MM-DD`；单股一次请求返回多日历史，并非批量多股或逐日请求。C784 三次串行请求分别 1543.602、1756.324、805.645 ms，采集请求间至少间隔 1.1 秒；此样本不能推导服务端配额。现行串行限速获取，已完成股票每日用 size=5 复核最近五个交易日；请求连接/读取超时 5/15 秒。供应商安全限速 UNKNOWN-D1（runner 查）。响应 `Date` 为 YYYYMMDD 字符串数组、`DDJE` 整数数组、`ttag` 浮点、`errcode` 字符串；按索引对齐，只保留目标日及以前的有效日期，缺值/非有限值为 NULL。现行接受 errcode 缺省、空、0、None，其余抛错；新合同应记录原始错误码并重试，缺省是否可视作成功 UNKNOWN-D2（runner 查）。最新返回日期早于目标日为 stale，不把历史响应当目标日成功。

现行聚合 `LAST(ts)` 得到每股水位，仅对水位早于目标日的股票请求，默认取返回的最近 250 个交易日，逐股写入并读回；现行会重写返回窗口而非仅写水位之后，也无法发现水位之前的洞。目标实现按日历和已存在股日集合补缺，只写缺失日期，单独处理历史洞；修订走 §7 失效流程。允许停牌 `suspended_no_same_day_dde`、已确认空响应 `kaipan_confirmed_empty_response`、上市前 `pre_listing` 三类候选白名单；每条必须带可信证据、日期、symbol 和原因，上市后新股无数据不能自动归为上市前。停牌证据应独立于日K依赖，来源 UNKNOWN-D3（runner 查）；空响应是否足以证明业务上可豁免 UNKNOWN-D4（runner 查）。未知失败绝不能自动白名单。

日完整的严格候选定义：目标日冻结的合格股票集合中，每股有有效 DDJE 行或经证据确认的白名单，且无未解决缺口；记录 expected、有效行数、豁免和未解决集合，去重及防止重复计数。没有经验证的覆盖阈值或权威股票集合/白名单合同，数值阈值 UNKNOWN-D5（runner 查）；在确认前不发布 FORMAL，不能拿 90/90 样本或单纯总行数证明全市场完整。单股请求默认最多 3 次，退避 0.5、1 秒，指数增长上限 30 秒；逐股失败不阻塞其他股，持久化已成功进度后整体返回可重试失败。F1 唯一写 `daily_dde_v1`；F2 和读取方只读。旧完整版本可作为明确标记 PARTIAL/STALE 的降级视图，保留原日期、版本与 available_at，不冒充当日 FORMAL，也不能满足 F2 的 FORMAL 依赖。

DDE 为历史数据；F1 事件 `daily_dde_0200` 每个日历日 02:00 触发，目标为前一交易日，截止 06:30，无日K gate。

## §3 F1 存储与日标记

现有 TD DDL（数据库须预先存在）：
```sql
CREATE STABLE IF NOT EXISTS core_data.daily_dde_v1 (ts TIMESTAMP, ddje DOUBLE, version VARCHAR(24)) TAGS (symbol VARCHAR(8));
```
拟新增日标记表（单日发布记录；字段均为目标设计）：
```sql
CREATE TABLE core_data.daily_dde_days (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), quality VARCHAR(16), expected_count INT, row_count INT, exempt_count INT, unresolved_count INT, universe_ref VARCHAR(256), missing_whitelist VARCHAR(16384), sha256 VARCHAR(64), available_at VARCHAR(40));
```
`row_count` 只计目标集合中 DDJE 非 NULL 的唯一 symbol；豁免不得与有效行重叠，unresolved 为其余实体数；`universe_ref` 固定股票集合版本，白名单记录每项证据引用，超出列容量不得截断后发布，需外置清单并引用（实现卡设计）。先完成行写入和核验，再发布标记；`available_at` 是实际可见时间。TD 同 ts 覆盖及原子多版本发布未解决（框架 U2），DDL 不是隔离方案，解决前不得生产部署。

现行 Redis `core:cache:data:daily_dde:{date}` 为 hash，field 是 symbol，value 为紧凑 JSON 的 `date/ddje/symbol`，TTL 86400 秒，数据来自 TD 目标日投影，可从 TD 重建。C784 未采集 Redis 原文，故以下**不是实测 Redis 行**，而是依照现行序列化格式、以 C784 原始上游首对值构造的示例；真实 Redis 原文 UNKNOWN-D7（runner 查）：
```json
{"date":"2026-11-06","ddje":18556900.0,"symbol":"600519"}
```
C784 快照 DDE 为 1,320,530 行、331 日、5,223 股；迁移只读逐日按 symbol 去重计算有效行数并生成历史标记候选，不重新获取。须对每个历史日期关联当日股票集合和已证实白名单；只有计数而无集合或证据时标记 PARTIAL/UNKNOWN，不升级 FORMAL、不虚构历史 available_at；源表不做改写。历史视图按可证明的可见时点处理，未知时不得用于过去 as-of。

## §4 F2 派生合同

已实现独立数据函数 `dde_derived`，按相同 symbol、交易日对齐可用的 L0：`ddje_ratio = ddje / amount_yuan`，DDJE 暂按元、中等置信度；amount_yuan 为元，分母为 0 或 NULL、DDJE 为 NULL 或任一输入非有限时输出 NULL、质量 PARTIAL，不补零。比值无量纲，不称作旧 DDX。C788 20 股 × 60 日 1,200 对，绝对比中位数 0.0491233、P99 0.202047、最大 0.401812、无大于 1；依据净额为成交额子集的假设，仅支持中等置信度，不是权威单位证明。

逐日独立，无窗口、递推、预热或跨日状态；每次整日重算，同时间戳覆盖，重复运行幂等。缺依赖不设门禁：缺失股票输出 NULL+PARTIAL 并记录原因，两边均无行返回 degraded，不发布日标记。DDL：
```sql
CREATE STABLE core_data.dde_indicators_v1 (ts TIMESTAMP, ddje_ratio DOUBLE, version VARCHAR(24), algo_version VARCHAR(32), input_refs VARCHAR(4096), available_at VARCHAR(40), quality VARCHAR(16)) TAGS (symbol VARCHAR(8));
```
`input_refs` 保存两输入数据集、日、版本和可见时间；结果完整性须另有按日完整标记，最后发布，不能以数值行是否存在代替质量。价格量簇只依日K及因子，DDE 比率另需独立外部 DDE，触发与失效不同；按框架 §5 判定树分簇。若 DDJE 单位被证伪，暂停旧算法发布，冻结更正换算及证据、升级 `algo_version`，对所有受影响股日全量重算并重新发布；旧版本保留血缘，切换仍受 U2 约束。

## §5 依赖与单独运行

F1 无上游数据集依赖，仅依开盘啦可用性、交易日历和独立股票集合；F2 默认仅检查本地已发布、as-of 可见的完整 L0，不取上游。拟新增 registry 声明（`window_days` 为交易日）：
```json
{"id":"daily_dde","requires":[],"produces":["daily_dde"]}
{"id":"dde_indicators","requires":[{"dataset":"daily_dde","window_days":1,"min_quality":"FORMAL"},{"dataset":"daily_kline","window_days":1,"min_quality":"FORMAL"}],"produces":["dde_indicators"]}
```
F2 检查当日日标记完整、版本、实体、质量和可见时间；缺依赖返回 exit 30，`--pull-upstream` 显式启用后才可调用 F1 或日K获取函数，再重新检查，仍缺则返回 30。F1 也可单独请求指定日期和股票，不因日K未完成而阻塞。输出合同为拟新增：

| exit | status / error_code | 语义 |
|---|---|---|
| 0 | ok / 空 | 完整成功 |
| 10 | degraded / STALE_VERSION | 仅旧版本、PARTIAL，非当日正式结果 |
| 20 | not_applicable / ALREADY_SATISFIED | 相同血缘和算法已完整提交 |
| 30 | failed / NOT_YET_AVAILABLE | 依赖缺失，可重试 |
| 40 | failed / CONTRACT_ERROR | 合同错误，不重试 |
| 64 | failed / CONFIG_ERROR | 参数或配置错误，不重试 |
| 75 | failed / DDE_SYNC_INCOMPLETE_RETRYABLE | 上游或写入暂时失败，可重试 |

新增代码须登记 error_codes 和运行时解码矩阵；以下是格式示例，非实测缺失：
```json
{"contract":1,"status":"failed","error_code":"NOT_YET_AVAILABLE","retryable":true,"artifacts":[],"detail":{"missing":[{"dataset":"daily_dde","trade_date":"2026-11-06","reason":"day_not_formal"},{"dataset":"daily_kline","trade_date":"2026-11-06","reason":"day_not_formal"}]}}
{"contract":1,"status":"ok","error_code":"","retryable":false,"artifacts":[],"detail":{"dataset":"dde_indicators","trade_date":"2026-11-06","quality":"FORMAL"}}
```
拟议 CLI，尚未实现：
```sh
python -m engine_core.daily_dde --date 2026-11-06 --symbol 600519
python -m engine_core.dde_derived --date 2026-11-06
python -m engine_core.dde_derived --date 2026-11-06 --pull-upstream
```

## §6 编排与降级

F1 `daily_dde_0200` 每日 02:00 执行至 06:30，串行限速并以 size=5 复核近期数据；目标为前一交易日，失败在窗口内重试。F2 触发由 F1 之后的时间槽决定，接入驱动器另卡实施。

对应 registry 项见 §5；F1 唯一来源开盘啦，失败后保留进度并重试，旧完整结果仅以 PARTIAL/STALE 降级展示；F2 按已有输入计算；缺失股票输出 NULL+PARTIAL，两边均无行返回 degraded。无替代来源，不把降级语义当成同日事实。

## §7 增量、失效与回填

每日 F1 只写当日缺失新行，F2 两输入就绪后只计算当日；同血缘、算法版本、参数与完整发布均一致才跳过。建议保留 F1 首次默认 250 交易日：兼容现行接口及已部署补缺范围，框架 120 日是其他簇窗口建议而非 DDE 预热需求；F2 无预热，对指定历史区间有配对依赖即逐日计算。回填按日历和实际上市区间，从最早可用日到目标日，记录缺口，非交易日不伪造行。

日K 的 `amount_yuan` 修订后按 symbol/date 使对应 F2 结果失效，含修订当日重算并更新 input_refs、版本与日标记；DDJE 修订同样只使对应股日的 F2 失效。现行水位跳过已完成股票，不能检测供应商追溯修订；修订频率、可用版本和检测窗口 UNKNOWN-D8（runner 查），检测能力未就绪前不得声称自动发现。F1 接到经证实修订后唯一负责发布新 L0 版本并通知 F2。

## §8 验证与验收

实施卡先写能在旧行为失败的测试，再实现；解析和计算使用真实上游原始响应，替身仅回放真实响应。C784 的 600519/000001/688001 三股最近 30 日对应的原始响应按 `examples/capture_real_fixture.py` 规定原样收入 `tests/fixtures/real` 并登记中央 `PROVENANCE.json`，保留来源和哈希；已有采集产物是否符合该入口的原始采集与登记要求 UNKNOWN-D9（测试负责人查），不得裁剪响应伪称原样或手写数据冒充实测。C788 `td_raw.jsonl` 中按股日配对的 DDJE 和 amount_yuan 作为真实数值证据，需确认能否由规定采集入口获得并登记；负值样本 300750 2026-10-21 为 -1340525000 / 32439007670.33。

fail-first 清单：
1. F1 日标记：真实响应回放与真实股票集合/证据快照配合，验证去重、NULL、停牌/新股/空数据证据、未解决缺口阻止 FORMAL，验证历史计数不自动转正式。
2. F2 用 C788 实际配对断言比值及符号；真实样本具备对应边界才验证 0/NULL，缺少时标 UNKNOWN 并由 runner 捕获真实边界，禁止手写假行宣称通过。
3. 任一依赖缺失、PARTIAL、晚于 as-of 均返回 30 及具体 dataset/date，默认不触发网络；显式 pull 才调用上游并复查。
4. 缺股增量、单股失败重试保留已写进度、stale/errcode、日K 与 DDJE 修订失效、单位变更全量重算。
5. 在隔离的 `core:` 测试键验证 Redis 清空后从 TD 重建，逐字段与 TTL 对账；不得删除其他命名空间或把测试清空扩散到生产键。
6. F1 不依赖日K而独立成功，F2 两依赖就绪后单独运行；验证编排先后和失败降级、唯一写入方及 U2 发布隔离。

## §9 迁移

`daily_dde.py` 保留开盘啦获取、逐股写入读回和水位恢复，补洞检查、日完整标记、修订处理与仅缺失写入；去除对日K数据的依赖，停牌证据须另有来源。`daily_dde_sync.py` 适配统一退出码、标记及显式参数；新增 F2 独立 handler 与血缘写入。registry 15:12、20:30 与 timeline 21:00 及日K gate 须统一为 §2 的建议并在时点证据到位后定稿。既有读取方及外部消费者清单 UNKNOWN-D10（runner 全仓与部署配置查）；须逐个双读比对与迁移，旧路径仅在消费者完成切换、真实数据对账和回滚窗口通过后删除；不得删除唯一 F1 写入能力。

## §10 与框架差异、缺口及自检

框架 DECISION-2 不变；本细则补充两个独立函数、日标记候选和具体时点建议。框架 120 日是建议窗口，F1 延续 250 日，F2 单日；既有代码目前缺日标记、血缘版本隔离与依赖检查，本设计不声称已实现。框架 U2 原子发布和 U13 最终失败降级未关闭；单独编排时 F1 从现有日K gate 解耦是拟议改造。C600 早期卡声称其他字段存在，而 C784 真实响应未观察到，遵循 DECISION-2 不复现。

自检：F1 覆盖 §1–3、§5–9：获取、完整性、存储、独立运行、编排、增量、验证和迁移；F2 覆盖 §1、§4–9：公式和单位、DDL、依赖及独立运行、编排、失效、验证和迁移。所有示例中的新字段和命令均为提案。

## §11 UNKNOWN 与待查清单

- UNKNOWN-D1：runner 查供应商安全限速和并发。
- UNKNOWN-D2：runner 查缺省 errcode 是否为成功。
- UNKNOWN-D3：runner 查可独立于日K的停牌可信证据。
- UNKNOWN-D4：runner 查空响应能否作为业务完整白名单。
- UNKNOWN-D5：runner 和数据负责人查目标日股票集合、完整覆盖阈值及白名单依据。
- UNKNOWN-D6：runner 查供应商当日数据稳定时刻；编排负责人确定 registry/timeline 与截止时间。
- UNKNOWN-D7：runner 查保存证据中的实际 Redis JSON 原文。
- UNKNOWN-D8：runner 查供应商历史修订行为、版本和检测窗口。
- UNKNOWN-D9：测试负责人查 C784/C788 原始数据能否通过规定采集入口合法入库登记及零/NULL 边界真实样本。
- UNKNOWN-D10：runner 查全部现有读取方、外部消费者和迁移顺序。
- UNKNOWN-U2：TD 负责人解决同 ts 物理版本隔离、原子发布与 as-of。
- UNKNOWN-U6：runner 查 DDJE 权威定义及单位；ttag 语义仍未知，不存。
- UNKNOWN-U13：编排负责人验证最终失败降级机制。
