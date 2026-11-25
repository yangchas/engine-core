# TASK-008 Q2Frame → Core Engine / Auction Timeline audit

审计日期：2026-09-29（Asia/Shanghai）
范围：只验证真实 t1-v2 Q2Frame 事件时间回放进入单个 Core Engine，并将已有 Engine 快照送入 `AuctionTimeline` / `AuctionAnchorRevisionV1`。不代表 TASK-008 整体完成或实盘等价。

## 主线判断

本次工作没有转向积压排查、生产改造或纯文档整理；它补的是 Core 迁移链上一个具体缺口：真实 Q2Frame 进入 Core 后，竞价锚点尚未通过项目已有的版本化时间线合同记录。本次只在 Core runner 中接入该合同，并用冻结的真实生产回放产物复跑。

仍遵守原边界：不接 Rabbit consumer、不改变 ACK、不写生产 Redis/TD、不改生产服务、不发送 effect、不把 REPLAY 宣称为 NORMAL。没有扩大任务板或开启后续阶段。

## 输入与可追溯性

- 交易日：`2026-09-18`
- 输入：`/home/exedev/validation/td-rabbit-phase-c-q2-full-20260924T014403+0800/q2frame.jsonl`
- 输入来源记录：t1-v2 exact-release 的本地 Q2Frame 输出，基于真实 TD 历史回放
- 输入 SHA-256：`43b94930f3ee58d1d796cd1e3a1f3e66243801b6c9bdbf2864f69d16949e12ae`
- 运行模式：`REAL_T1V2_Q2FRAME_EVENT_TIME_REPLAY`
- 回放边界：逐条消费冻结文件中的完整 Q2Frame，不按任意行数抽样；竞价观察点之前只处理满足时间边界的 frame。
- 回放输出：`/home/exedev/validation/task008-q2frame-auction-engine-20260929T024902+0800/q2frame_auction_engine_shadow.json`
- 输出 SHA-256：`047b5c982a7fb0e7481896a73d5a89f9091fcdde7c7678dba96f6b8a8ecdac77`

## 实测结果

- 冻结文件：1,205 frames、1,226,603 updates、5,221 个唯一 symbol；空 frame 0。
- 输入含亚秒 update 时间数：0。不能从这份数据证明 `09:25:06.xxx` 的截断行为。
- 到 `09:25:06` barrier：605 frames、226,253 updates；单个 Engine；605 reducer revisions、608 processed signals。
- `09:25:06` 首轮快照使用的最新源事件时间为 `09:25:04`。下一个完整 frame 标记为 `09:30:00`，含 5,221 updates，被排除在 09:25:06 barrier 之外。不能把这个后续 frame 内按 symbol 的旧时间戳倒灌进已冻结快照。
- ordered 与 repeat pass 的输入处理数、锚点证据、Clock、Engine 最终 hash 一致；最终状态 hash：`70a8c9c215e6e8d06ee657244d4ef0cdd6ae94b413b6c9f5fef317d3788a12e6`。

三个锚点都经 `AuctionTimeline` 产生 `AuctionAnchorRevisionV1` revision 1，`observed_at_ms=null`（未伪造历史可用时间）：

| 锚点 | 评估/冻结时间 | 输入源事件时间上界 | 覆盖 | 事实状态 |
| --- | --- | --- | --- | --- |
| 09:20 | 09:20:03 | 09:20:03 | 5,221/5,221 | 5,221 `PENDING` |
| 09:24 | 09:24:10 | 09:24:10 | 5,221/5,221 | 5,221 `PENDING` |
| 09:25 | 09:25:06 | 09:25:04 | 5,221/5,221 | 5,221 `PARTIAL` |

以上 coverage 只是“相对于该冻结 Q2Frame 文件中的 5,221-symbol universe”，不是全市场完整性证明。Engine 输出保持 `FACT_ONLY`；`normal_opening_acceptance=NOT_EVALUATED`。

## 事实边界与剩余未知

- `rabbit_arrival_order=UNKNOWN_NOT_INFERRED`；TD/Q2Frame 事件时间顺序不等于 Rabbit 到达顺序。
- `rabbit_delivery_membership=UNKNOWN_NOT_INFERRED`；历史 Q2Frame 不能证明实时队列成员和批次边界。
- `historical_available_at=UNKNOWN_NOT_INFERRED`；未将源事件时间冒充 Redis 可用时间。
- 输入没有亚秒 update 时间，实盘到达延迟、09:25 后晚到消息以及 06 秒附近的真实混合行为均未由本次数据验证。
- 0920/0924 锚点输出的是当时 Engine facts；现有策略事实状态是 `PENDING`，不能误读为已验证的开盘结论。
- 冻结输入中 `frame_second_mismatch_count=1854` 出现在后续 09:30 frame；不把该 frame 内较早的 per-symbol 时间回填到 09:25 barrier。
- 因而此证据证明“真实 t1-v2 Q2Frame 可驱动 Core 单 Engine/时间线的可重复开发回放”，不证明 live queue 等价、正常开盘通过或生产策略有效。

## 副作用、验证与状态

- 生产副作用：`NONE_OBSERVED`；本次只读本地冻结 artifact 并在内存中运行 Core。未访问生产 Redis、TD、Rabbit 或 systemd。
- 全量测试：`708 passed, 3 warnings`。
- `compileall`：PASS。
- `git diff --check`：PASS。
- TASK-008：保持 `PARTIAL_EVIDENCE` / `REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`；不得据本报告标记完成。
- `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`；M3-1 仍为 `BLOCKED`，`TD_WRITE_HEALTH=UNPROVEN`（均为本任务范围外门禁）。

## 变更范围

本报告随本次实现提交的只有：

- `examples/run_task008_q2frame_auction_engine_shadow.py`：将同一 Engine 的锚点快照输入既有 `AuctionTimeline`，输出版本化 revision evidence。
- `tests/test_task008_q2frame_auction_engine_shadow.py`：验证 revision、时间边界、覆盖和未伪造 `observed_at`。
- 本 handoff：记录真实输入、运行证据和限制。

未改写其他已有工作区修改；未推送、合并或部署。
