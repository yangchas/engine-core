# Engine Core 当前进度自审与 CC 决策交接

审计日期：2026-10-11（Asia/Shanghai）
范围：`/home/exedev/repos/engine_core` 当前工作树、项目计划/任务板及已登记证据。
本轮未连接 Redis、TDengine、RabbitMQ，未检查或改变生产服务，未改业务代码、未提交。

## 先给结论

**架构方向大体符合“低耦合、模块分离、可容忍部分数据缺失”的目标；回放开发链已可用于一部分真实输入上的功能演化，但还不能代表实时 Rabbit/生产 Q2 行为。项目有陷入“证据与诊断不断加厚、核心迁移闭环没有同步收口”的流程局部最优迹象。**

这不是“系统被门禁卡死”的简单结论：任务板明确把
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS` 与
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN` 分开，允许已覆盖字段继续开发；计划也明确要求普通输入缺失降级为 `PARTIAL/UNKNOWN`，只有日期、schema、单位、未来时间和副作用等安全违规才硬失败。问题在于 TASK-008 自 2026-09-20 起持续 `RUNNING/PARTIAL_EVIDENCE`，最近新增证据主要是离线、同一 Core 实现上的跨日组合复核；而 Rabbit delivery/arrival、producer build、当时 Redis 可见性等未决事项不能靠重复离线报告关闭。应拆开开发可用性与实盘等价性，不应等后者才推进前者。

## 已做到的程度

| 目标 | 当前证据 | 结论 |
|---|---|---|
| Core 与生产 IO 解耦 | `src/engine_core` 未检出 Redis、TD、Rabbit 客户端或生产服务模块导入；项目文档把 Rabbit/ACK、Redis/TD owner 留在 Runtime/t1-v2。Core 主体是 Python 纯计算/事实/回放模块。 | **方向 PASS**。静态导入检查不等于部署集成证明。 |
| 真实数据可用于开发回放 | 计划记录了真实 TD 单个 3 秒 SELECT 的切片验证；也记录过同日真实 TD 经 exact t1-v2 到隔离 Redis、再由 Core 读取的历史桥接。TASK-008 当前 pinned Q2Frame 的 2026-09-23 全窗回放为 500 帧、1,209,672 次更新、5,222 个观测 symbol、78 空帧，ordered/repeat 最终 hash 相同。 | **开发用途 `USABLE_WITH_LIMITS`**。不同证据路径/日期/产物不能拼成同一运行的端到端证明；当前 500 帧复播使用已保存的 Q2Frame，不是本轮重新读 TD 或重跑 t1-v2。 |
| 输入不完整时仍能继续分析 | opening 可逐 symbol 产出 `READY/PARTIAL`，缺失锚点可保留 `UNKNOWN`；2026-09-23/29/30 报告分别为 12/12/7 个 stale/partial，而其余 cohort 仍有事实输出。恢复实现对 malformed/冲突成员做局部隔离；测试覆盖有效兄弟成员继续处理。 | **部分鲁棒性已实现**。这些不是实时 provider/Wencai 集成证明；stale 代表时间戳年龄，不证明个股没有 tick。 |
| 09:25 冻结与回放时间语义 | 计划区分 live 墙钟触发与 replay 事件时间重建，保留 `05/06/07` 秒证据，空帧推进虚拟时钟；旧锚点需独立冻结，不能从后续滚动 Q2 拼回。 | **合同/局部路径已实现并有历史真实数据实验；live 到达集合仍 UNKNOWN**。TD event time 不能证明 09:25:06 前 Rabbit/Redis 是否已可见。 |
| 当前代码回归 | 本轮在当前脏工作树执行：`pytest -q -p no:cacheprovider` → **1001 passed, 3 warnings, 6.86s**；`compileall` PASS；`git diff --check` PASS。 | **本地回归 PASS**，不能据此宣称生产正确或实盘等价。仓库文档最新登记仍写 933 tests，已落后。 |

## 未达到的主目标与主要矛盾

1. **Rabbit 同形不等于 Rabbit 同批/同序。** TD 3 秒 frame 是读取时间边界；生产 Rabbit delivery 可能拆批并混入相邻事件时间。计划明确不伪称两者批次相同。生产 `DataBatch` delivery membership、arrival sequence、completion watermark、历史 `available_at` 未形成可复核 capture，故 exact live-input parity 未证明。
2. **相同 t1-v2 计算仍受事件阶段/批边界影响。** 计划记载 `EngineCore::on_batch` 以 batch `logical_ts_ms` 决定阶段；跨 09:25/09:26/09:30 的片内 tick 不能仅按行数相等判定一致。已有 barrier-aware TD replay 实验，但与 exact production Rabbit delivery 和 live 09:25:06 freeze 仍有边界。
3. **最新两次 parity 的独立性有限。** 2026-10-11 的 09-23/09-29 复核均使用 sealed Q2Frame 和旧 Core report；对比路径共享 Core 的事实/策略实现，重复运行只抽样 312 个 symbol。它们能证明特定输入、特定输出组合稳定，不能验证 t1-v2 producer、Redis 当时状态、Rabbit 或全市场分母。产物上游构建身份仍 UNKNOWN。
4. **任务颗粒度/推进方式有局部最优风险。** TASK-008 长期把“回放开发可用”“全市场/生产等价”“正常开盘验收”放在同一运行卡下，且持续增加 stale/universe/parity sidecar 与报告。最新 CC 决策已经指出继续重复离线日期的边际收益下降；缺少新 source/provenance 的离线复跑不能解决核心未知项。
5. **当前工作树不可作为唯一、干净的交付身份。** branch 和 origin 开发分支均为 `f7535d18...`，仅一个 worktree；但 `git status` 有 **33 条**变更/未跟踪路径，横跨任务文档、运行器、测试、证据、一个新 reconciliation 模块等。最近 parity manifest 也记录了 dirty-worktree fingerprint。这不应通过 reset/clean 解决；需由 owner 决定如何分组提交，但在此之前报告必须明确指向 dirty tree，而不能只指 HEAD。

## 门禁判断

- **合理硬边界：** 日期/schema/单位错误、时间倒退或未来时间错误、副作用/写错目标、生产 ACK/服务/TD writer 误触。这些保护数据语义和生产安全。
- **合理的证据边界：** `NORMAL`、Rabbit arrival parity、生产 09:25:06 cohort 与 source build 归属需要相应实时或可追溯证据；不能由服务 active、event-time 排序或相同 Core 两条路径代替。
- **不应成为开发硬门禁：** 0920/0924 缺失、少数 stale/invalid symbol、历史 `available_at` 未知、Rabbit arrival 未知或全市场分母未知，不应阻止独立字段/事实路径的离线开发。现有状态标签总体遵守这条，但单卡长期保持 RUNNING、持续把诊断扩充到 TASK-008，容易在流程上变相阻塞。
- **建议 CC 判断是否过严：** 当前 12/5222 stale 导致 projection 标 `PARTIAL`，但有 5210 `READY` 仍继续输出；这看起来是质量标注而非停止门禁，符合鲁棒性目标。不要为了让 overall status 变 PASS 而把 stale 改成 fresh 或放松 source safety。

## 自审建议（待 CC 决策，不是执行授权）

1. 保留上述生产等价声明为 `UNPROVEN`，不要继续用更多同型 sealed-Q2Frame 跨日复跑追求“证明”。
2. 将任务拆成独立可推进的“已覆盖输入上的 Core 功能演化/回归”和“Runtime/Rabbit/09:25 live parity 未决”两条状态；前者不等后者。避免一个 TASK-008 卡片成为所有开发的总闸。
3. 下一个技术任务只选一个具体能力缺口，优先围绕真实 TD 逐 3 秒输入→选定版本 t1-v2→隔离 Redis/Q2Frame→Core 的一条可重复纵向路径；先复用已有 sealed 输入做离线回归，再只有在有明确新问题且授权/安全方案齐备时取新源。不要再建只增加报告而不改变决策的 sidecar。
4. 在 owner 决定提交分组前不改动这 33 条工作树状态。完成审计后只更新过时的测试数/工作树说明也需单独决定，避免把新审计报告误当作代码已提交或主线已完成。

## 可让 CC 复核的仓库内材料

- `AGENTS.md`
- `docs/PROJECT_KNOWLEDGE.md`
- `docs/work/TASK_QUEUE.md`、`docs/work/CURRENT_TASK.md`
- `docs/work/plans/TD_RABBIT_GLOBAL_3S_REPLAY.md`
- `docs/evidence/task008-live-node-parity-20260923-20261011.md`
- `docs/evidence/task008-live-node-parity-20260929-20261011.md`
- `docs/evidence/task008_opening_stale_symbols_crossdate_20261010.md`
- `src/engine_core/opening.py`、`auction_timeline.py`、`replay_session.py`、`session_runtime.py`、`recovery.py`

`/home/exedev/validation/...` 中的原始 JSON/sha 清单不在本仓库内，CC 当前 Read-only 会话不能直接读取；上文中对其数字的引用来自仓库内已登记证据。若 CC 的结论依赖原始工件，应明确标记为“仅执行侧核验”，不能冒称已独立重算。

## CC 同会话独立决策与执行侧复核

2026-10-11 已恢复既有 Claude Code 会话（未新建会话），只给 Read 工具和本仓库只读目录。CC 读取了本报告、任务板、主线计划、live progress 审计及近期任务记录；没有读取 `/home/exedev/validation` 原始工件，也没有重新检查 `src/`。因此它是**文档/治理层独立审阅**，不是源码审计或原始数据重算。

CC 核心决定：

- 项目确有局部最优迹象，但主要是“同一 Core 实现对封存 Q2Frame 的离线证据循环”，不是缺失 Rabbit 证明就技术上禁止 Core 开发。
- 不应再重复做全窗跨日离线复跑；此前真实 TD→exact t1-v2→隔离 Redis→Core 的纵向路径已经做过多轮。
- TASK-008 将开发可用性、Core/producer 对照、Rabbit/live 等价及 NORMAL 验收混在一个长期 RUNNING 卡片中，是流程层的推进摩擦；技术状态应继续区分 `USABLE_WITH_LIMITS` 与 `UNPROVEN`。
- CC 认为最小的新工作是从已有材料做 2026-09-24 live progress 与 TD 回放记录量的只读对账；对不上即报告 `UNKNOWN` 并停止，不加新 consumer、不接新服务、不改状态。
- CC 补充了此前自审低估的跨实现证据：2026-09-29 09:32 producer cutoff 与 Core 在 5,211 个共有新鲜 symbol 上的相关字段有比对记录；2026-09-30 09:25 Core/TD 可比正价格为 5,030/5,030，NULL 行为 190/190。它们是有价值的**范围受限跨路径证据**，不等于 Rabbit 到达顺序、全市场覆盖或生产 NORMAL 验收。
- CC 指出 `examples/run_task008_live_node_replay_parity.py` 调用 `examples/run_live_morning_shadow.py` 的 `build_node_evidence`，说明样例层被当作复用 API；此处存在应用组合层耦合，但不是 Core 引入生产 IO 的证据，也不应为此阻塞其他开发。

### 对 CC 新发现“记录量差异”的证据口径校正

仓库记录中确有明显的计数差异：2026-09-24 live progress 首末计数增量 `source_in=1,930,517`（09:15:01–09:39:58 墙钟进度行），同日 TD→exact t1-v2 窗口 `[09:15,09:40)` 的 `SELECT COUNT(*)=1,194,572`。拆分阶段也有类似量级差异。这个发现值得优先做只读对账，不能忽略。

但这两个数**不是已证明的同一记录集合的计数**：live `source_in` 是两条稀疏 progress 行之间、已交给 builder 的累计记录增量；TD 数是事件时间半开查询结果。progress 的 `last_ts_ms` 只描述样本时点附近的最后源时间，没有逐 delivery 的成员/时间范围；窗口起止也并非完全相同。因而 1.62 倍只能称为“不同计数口径下、同日相近窗口的未解释计数差异”，不能直接断言回放漏掉/多出 62% tick，也不能由聚合比值推断重复、丢失或 Rabbit 特有数据。CC 提出的对账若实施，应使用仓库已保留材料、按可得的 `last_ts_ms` 粗分段并显式标注边界偏差；若没有 per-slice count 或进度原文，结果就保持 `UNKNOWN`，不要通过重新连接生产源来补齐。

CC 关于09-23 09:32:10 cutoff 的纠正也成立：包含 seq 343 的 09:32:06–09:32:09 frame、排除 seq 344 的 09:32:09–09:32:12 frame，**不能证明**被排除 frame 内不存在 `<=09:32:10` 的行。此点影响那个 cutoff 的输入完整性声明，但不推翻 09-29/30 的其他同输入字段对照，也不是要求所有离线开发停摆的理由。

本轮未执行 CC 建议的计数对账，未改变 TASK-008 或任何生产/部署状态；仅记录审计决策与口径限制。
