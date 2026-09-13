# engine_core 测试范围与真实数据审计（2026-09-13）

## 结论

当前分支 `codex/feature-session-engine-integration` 的提交 `eb39f5e` 在本地通过 `299 passed`、`compileall` 和 `git diff --check`。这些测试主要是纯函数、注入式 Provider、冻结 fixture 和确定性 replay；它们不是每次运行都连接生产数据。

本次在 `cobra-ion` 使用当前提交的临时副本执行了真实只读探针：

- `daily_kline`：真实 TD SELECT 返回 2026-09-10 的 600519、000001 两行；日期和字段归一化成功，但因没有历史 `available_at` 证据，结果按合同为 `UNAVAILABLE`，不是 Provider 失败。
- `auction_snapshot_v2`：真实 TD SELECT 返回 2026-09-10/600519 的 0920、0924、0925 三个投影行。0920→0924 为 `PARTIAL`（价格缺失），0924→0925 为 `READY`；P/M/RB/RA 与 pressure 事实可重复，状态保持 `FACT_ONLY/OBSERVE`。
- Redis Q2：当前为周日，`q2:active:*` 为空；真实 adapter 正确返回 `MISSING/EMPTY_UNIVERSE`，两次 Engine 运行 hash 相同。不能把空 cohort 当作 live coverage 通过。
- Cobra 同一提交重复上述三项：`299 passed`；TD 竞价 shadow hash 为 `abb13e59f6c087d20cd0fae9b92b29476db9e1cb0e1d0fd48222495b1a23fb32`，昨日数据仍为 `UNAVAILABLE/available_at_unknown`，Q2 仍为 `MISSING/EMPTY_UNIVERSE`。

## 测试真实性分层

| 层级 | 范围 | 证据 |
| --- | --- | --- |
| Unit | Contract、Clock/Calendar、Q2、Window、Data、Facts、Engine、Replay | 本地 pytest 299 passed |
| Fixture composition | 真实捕获/冻结 JSON、Q2Frame、TD 行结构 | `tests/fixtures` 与 `test_real_*` 中的 override/fake |
| Real read-only | TD `daily_kline`、TD `auction_snapshot_v2`、Redis Q2、既有第三方 connector probe | Cobra 临时副本；无 Redis/TD 写入、无 Rabbit ACK、无通知 |
| Production chain | Rabbit 原始 batch membership、t1-v2 decode/trigger/writer 同批关系 | 当前无新增 consumer/tap；仍为 `UNKNOWN`，不能由 TD 反推 |

## 已补的基础轮子覆盖

`normalize_auction_change_ratio` 原先只有实现没有独立单测；本次增加 ratio、百分点评分、basis-point、负值、零值、缺失、非法和非有限输入测试。

同时收紧 Q2 `trade_date` 边界：adapter/projection 在读取前拒绝非严格 `YYYY-MM-DD`，避免 malformed date 在空数据场景被伪装成普通缺失。

旧发布包的纯 `build_anchor_shadow_evidence` 已对 Cobra TD 2026-09-10/600519 三个真实投影行执行：0920→0924 因价格缺失为 `unavailable`，0924→0925 的 amount/price/rest bid/rest ask/pressure 数值与 core 一致；legacy 的方向/撤单/比例标签仍不作为 core 策略 oracle。

## 当前仍未宣称的能力

- 当前 Redis 历史 auction key 已过期或仅有 Top-200 捕获，不能证明全市场 0920→0924 相邻快照。
- TD 查询顺序不代表 Rabbit 到达顺序；同毫秒同标的缺少 source sequence 时，任何 tie-break 都不能解释为生产因果顺序。
- 旧 `engine_next` 完整生产事实组装需要 Redis summary、anchor、mapping 与 TD 三锚点同时存在；当前历史捕获缺少 0924/完整同日 Redis，因此 legacy consumer oracle 保持 `UNKNOWN`。
- BaoStock/Kaipan/THS/问财连接性已在此前真实探针中观察到，但没有历史 `available_at` 证据的结果只能作为 oracle/fixture，不进入 Replay runtime。

## 下一步停止线

不新增 Provider、Replay 框架或 Engine 能力。下一个有效动作是获得一个真实交易日的 Redis Q2/auction 0920、0924、0925 只读捕获，或明确记录不可获得；随后再决定第一条 Auction Shadow 规则是否具备 legacy consumer oracle。

## 新增 Engine Integration 审计发现

当前 Engine 为保证同一 session 内 `evaluation_id` 只能注册一次，使用永久增长的
`_registered_evaluation_ids` 集合，并以 `evaluation_registration_limit=4096` 作为
硬上限。一个合成的 4097 次无数据 `PULSE` drain 已复现：第 4097 个评估触发
`RuntimeError: evaluation registration capacity exhausted`。这不是当前轮子测试的
失败，也没有在本次文档提交中擅自改变 ownership 语义；但它意味着当前内存 Engine
不能直接承诺覆盖完整长交易日的高频评估。进入正式替代前必须由 Engine Integration
阶段明确安全方案（例如按 session 生命周期分片或持久化去重游标），并补充长时段
容量测试；在此之前不得宣称 production replacement ready。
