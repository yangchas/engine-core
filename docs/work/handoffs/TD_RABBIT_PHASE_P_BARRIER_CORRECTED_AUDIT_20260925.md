# TD/Rabbit global replay — corrected source barrier audit

审计日期：2026-09-25（Asia/Shanghai）
状态：`PHASE_P_PARTIAL`

## 结论

对已提交的 t1-v2 开发分支实现进行了真实 TD → t1-v2 → 隔离 Redis → Core 只读读回。重放可以逐片读取、保留整片所有有效输入，并在三个业务时点插入独立 Clock；真实输入计数、重复输出和 Core 读回均有证据。但当前实现与旧 DB5/`task009k:` 基线并非全量语义相等，原因尚未证实，因此本次不能把旧的“基线 parity”结论沿用到新分支，也不把 TASK-008 或 Phase P 提升为 PASS。

```text
REAL_TD_TO_T1V2_REPLAY             PASS_WITH_LIMITS
ONE_SELECT_PER_3S_SLICE_CONTRACT   PASS_BY_SOURCE; REAL_ROW_COUNT_MATCHES
NO_ROW_COUNT_COMPUTE_CHUNK         PASS
ISOLATED_REDIS_REPEAT              PASS
CORE_REAL_REDIS_READBACK           PARTIAL
OLD_BASELINE_SEMANTIC_PARITY       PARTIAL / NOT_EQUAL
RABBIT_DELIVERY_OR_ARRIVAL_PARITY  UNKNOWN
TD_WRITE                           0
RABBIT_ACK                         0
PRODUCTION_SERVICE_CHANGE          NONE_OBSERVED
M3_1_NORMAL                       BLOCKED
TD_WRITE_HEALTH                    UNPROVEN
```

## 实现与构建

- t1-v2 分支：`codex/task-q2-pure-function`，提交：`e91a20a` (`fix(replay): emit auction barriers within 3s slices`)；仅本地提交，未推送、未部署。
- 验证二进制：`/home/exedev/validation/task009p-barrier-corrected-20260925T020200+0800/t1_v2_replay`
- SHA-256：`94291979508168ec3f2982666bb25f91851ed5c55ee7aecced14569e29033bc2`
- 全量 C++ 构建及内置 self-test 通过；`git diff --check` 通过。
- 修改限定于 replay slice/barrier、09:25:06 SnapshotTrigger 和 self-test；没有改 Rabbit consumer/ACK、TD writer、生产服务或 schema。

该实现保留每个 TD `[start,end)` 3 秒查询返回的全部行，不按行数拆分；每片正常产生一个数据 batch。仅当 09:20:03、09:24:10、09:25:06 屏障落在片内/片边界时，发出屏障前数据、独立 Clock、屏障后数据。`chunk_no` 固定为 0。秒级门槛按 `floor(ts_ms/1000)` 判断，因此 `09:25:06.197` 属于 06 秒；06 秒及以后事件在 Clock 之后处理。此顺序是事件时间重建，不是 Rabbit 到达顺序。

09:25:06 若恰好是 `[09:25:03,09:25:06)` 的右边界，当前 reader 可以在该片结束时先发出 Clock；不必为了产生 Clock 再查询 `[09:25:06,09:25:09)`。本轮真实运行仍到 09:25:09，随后读取到一个真实空片；该空片会推进 Q2/latest 的运行时间元数据，但不会产生 tick。

## 真实运行与重复性

- 源：TD `market_data1.stock_tick_v2`；交易日 `2026-09-23`；半开窗口 `[09:15:00,09:25:09)`。
- 203 个逻辑 3 秒片；有效输入 `212,022` 行。成功退出的 run2、run3 均报告 `source_in=ticks=212022`、`source_reject=0`、`batches=207`、`redis_cmds=364982`、`redis_committed=181667`、`td_sql=0`、`ack=0`。
- run1 的退出码未保留；其 Redis 命名空间已完整生成。run2 和 run3 的退出码均为 0。三个 run 均使用 Redis DB15 的唯一前缀：`task009pfix20260925:`、`task009pfix20260925b:`、`task009pfix20260925c:`，各有 5,233 个 key。
- 排除 `m2:runtime` 内受同一 Redis DB 其他 key 影响的 `redis_bytes` 后，三组 Redis key/value 规范化 SHA-256 均为 `ea53a68d85f4dade9c61bcbae6e7a29f5aae10a575f8ce58cb460ca71d882435`；pairwise keyset/value mismatch 均为 0。
- 三个候选前缀在生产 Redis DB0 的命中数均为 0。DB15 `rdb_last_bgsave_status=ok`、`aof_last_write_status=ok`。本轮未删除这些隔离证据。
- Core 使用只读 `RedisQ2ProjectionAdapter` 读取三组真实结果，均为 `expected=5222`、`observed=5222`、`missing=0`、`coverage=1.0`、`stale=154`、状态 `PARTIAL`；三组 projection hash 均为 `9d09efeed79ef3b50c3d7194c89231a0ccf5db7e1d127a9d1d412ef16e32f0a2`。此结果不是 NORMAL 开盘验收。
- 同一 Core 观察时刻/10 秒 freshness policy 下，旧 DB5/`task009k:` projection hash 为 `a4b1913d1fd63178395636399421ff894c7a81e70f99861a4d587fcaed507ec1`，与当前三次读回不同。

## 与旧基线的实际差异

对照 Redis DB5/`task009k:`（旧 exact-release 阶段证据）时，当前开发分支输出没有全量相等：

- Q2 key 集相同，但 5,206 个 Q2 hash 的值不同；差异主要落在 `br`/`ar`，另有少量 `am`、`a20`、`ts`、`ls` 差异。仅凭当前证据无法证明差异来自屏障/整片批次语义、同毫秒同股票的 TD 返回次序，还是源码/版本差异；不得把任何一种解释当作事实。
- A2 meta：0920 当前 `n=4872`、旧基线 `4873`；0924 当前 `5099`、旧基线 `5100`；0925 两边均 `5208`。各 TopN 的 symbol 集相同，但每个列表各有一条共同 symbol 的行值不同。
- `latest` 当前 meta 时间为 `09:25:09`，旧基线为 `09:25:03`。本轮将 09:25:06 后真实空片作为时钟事件处理，因此它能刷新 latest 的观察时间；它没有市场 tick。该元数据变化是可解释的时间语义差异，但不能解释全部 Q2/A2 数值差异。

因此本轮结论是“重复确定性 PASS，旧基线等价未通过/未解释”，不是回放失败，也不是 strict parity。下一步若继续应先定位上述逐字段差异及稳定源行身份；没有可证明的 TD row sequence 时，真实 arrival/order 继续保持 `UNKNOWN`。不应为了让 hash 相等而过滤、去重或补造行情。

## 追加审计：旧基线的 batch cadence 不同

后续核对旧 DB5/`task009k:` 的原始运行日志发现：旧基线处理同一交易日、同一 `[09:15:00,09:25:09)` TD 查询窗口时，最终为 `source_in=212022`、`batches=604`；其 progress 日志中每增加 10 个 batch，`last_ts_ms` 前进 10 秒，说明旧执行路径约为每秒一个逻辑 batch。证据：

- `/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202721825+0800/t1_v2_stdout.txt`
- `/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202721825+0800/run_meta.txt`

当前 3 秒路径对同一历史窗口查询 203 个 `[start,end)` frame、读取相同的 212,022 行；三个业务屏障将处理 batch 增至 207。证据：

- `/home/exedev/validation/td-rabbit-phase-p-barrier23-run-20260925T013000+0800/per_slice_stats_v1.json`
- `/home/exedev/validation/td-rabbit-phase-p-barrier23-run-20260925T013000+0800/phase_p_barrier23_summary.json`

因此，DB5 与当前输出的比较是不同逻辑 batch cadence 下的结果比较，不满足“输入边界相同”的严格 parity 前提。它仍然是有价值的差异诊断，但不能单独证明当前实现错误，也不能抹掉字段差异。当前 `br/ar` 差异是否由 batch cadence、屏障位置、TD 同时间行顺序或 source/build 差异造成，仍为 `UNKNOWN`；在取得真实 Rabbit DataBatch 的成员边界/投递证据前，不应把旧 Redis 输出当作同边界 oracle。

## 副作用与服务状态

- Redis 写入仅限获批的 DB15 唯一隔离前缀；生产 DB0 候选前缀命中为 0。
- t1-v2 汇总 `td_sql=0`、`ack=0`；没有消费 Rabbit。
- `engine-next`、`t1-v2-live` 均 `active`；检查时 `NRestarts=0`，磁盘 `/` 约 44% 使用、可用约 22G。
- 没有部署、重启服务、改 TD retention 或修改生产目录。

## 主线对齐

符合：Rabbit/TD 进入同一 t1-v2 状态机、TD 每次只查询一个 3 秒片、无行数 chunk、所有有效 tick 保留、Q2 写入仅隔离 Redis、09:25:06 由独立 Clock 冻结。

仍未证明：真实 Rabbit DataBatch membership、单个 Rabbit delivery 与 TD 片的形状等价、到达顺序/completion watermark、历史 `available_at`、实时可见集合、完整旧基线语义 parity 和 NORMAL acceptance。计划阶段仍为 `PARTIAL`，不自动推进下一阶段；`M3_1_NORMAL=BLOCKED`、`TD_WRITE_HEALTH=UNPROVEN` 保持不变。
