# Phase A — exact runtime release and auction Redis retry audit

审计日期：2026-09-25（Asia/Shanghai）
方式：ECC `contract-first` + `production-audit`；C++ 修复按 ECC `cpp-testing`
执行 RED → GREEN。只读检查生产 release；代码仅改 t1-v2 开发分支。

## 状态

```text
PHASE_A_SOURCE_AUDIT=PASS_WITH_LIMITS
PHASE_P=PARTIAL
TASK_008=PARTIAL_EVIDENCE
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

本次没有消费 Rabbit、ACK、写生产 Redis/TD、改 `schema.proto`、改生产目录、
重启服务或部署。Phase P 和 TASK-008 状态未升级。

## 当前运行版本身份

- `t1-v2-live=active`，`engine-next=active`；只读检查时 t1-v2 PID 为 304，
  `NRestarts=0`。
- `t1-v2/current` 指向 release `20260923_tdstop0945b`。
- 活动二进制 SHA-256：
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`，与
  release `build_info.json` 一致；`SOURCE_MANIFEST.txt` 校验通过。
- release 声明的 base commit `9fd4a42` 不在本地 t1-v2 Git object database；
  t1-v2 仓库没有 remote。故活动二进制已由 hash 锁定，但不能从 Git commit
  证明它与当前开发 HEAD 同源。

## Rabbit 输入：可证实的字段和丢失的 metadata

运行 release 的 `schema.proto` 定义了：

- `DataRecord`：`tss/lp/o/h/l/lc/a/v/p`、五档价格/数量及
  `symbol/exchange/market`；proto3 scalar 没有逐字段 presence 证据。
- `DataBatch`：`batch_id`、`records[]`、`sent_at`。
- `DataRequest`：压缩类型和 `compressed_data`。
- 外层 wire header 还包含 `batch_id/record_count/timestamp` 等传输 metadata。

消费路径实际观察到的行为：

1. 一次 `RabbitMqTickSource::next_batch()` 消费一个 AMQP envelope；
   `delivery_tag` 留在 source result 中供后续 ACK，不是 tick sequence。
2. 在本次审计所检查的 release/source revision 中，decoder 解析外层
   header 和 protobuf records；但只将 records 传给 `SourceTickBatchBuilder`。
   外层 `batch_id/record_count` 与内层 `DataBatch.batch_id/sent_at` 没有进入
   当时的 `TickBatch`。
3. 被审计的 release `TickBatch` 只有
   `mode/logical_ts_ms/wall_ts_ms/seq_no/ticks`。
   Rabbit 的 `logical_ts_ms` 由本批最大 tick `tss` 回填；`wall_ts_ms` 被外层
   header `timestamp` 覆写；`seq_no` 是进程内本地计数。
4. 字段 `p` 被 protobuf decoder 放入 `SourceTickRecord`，但
   `RawTickConverter` 不映射它到 `RawTick`；其业务含义本次不推断。
   `DataRecord` 没有 per-tick arrival、delivery sequence、completion watermark
   或历史 `available_at`。

因此，Rabbit primary 的 wire schema 已知，但完整的 production Rabbit
`TickBatch` metadata contract 尚未成立。真实 live payload/delivery 成员捕获仍
不可得；日志里的 progress/ACK counters 不能还原哪些记录属于同一 delivery。
用户报告每片约 3–4 个 delivery 继续记为 `UNVERIFIED_BY_RUNTIME_CAPTURE`。

## TD 输入与 Rabbit 的差别

- exact release 的 TD replay SQL 对 `stock_tick_v2` 每次按一个 3 秒时间范围
  查询；选出 tick 核心字段、五档和 `inst_vol/inst_amt/large_net`，不选
  `p/market/exchange` 或 limit 元数据；market/exchange 由 row converter 从
  symbol/source 推导，不能据此声称与 Rabbit 字段等价。
- 对实际 TD 表执行过一次只读 `SELECT * ... LIMIT 1`，只记录返回字段名，未
  打印/保存行值；返回 schema 有 32 列（时间、行情/五档、derived fields、
  symbol）。此结果不代表检查了 TD stable tags。
- TD 真实事件时间不携带当时 Rabbit arrival；按事件时间重建的 frame 和
  source message batch 边界不是同一个概念。现有 real-data evidence 仍有效，
  但不能升级成 Rabbit delivery parity。

## 09:25:06 实盘处理顺序核对

- live loop 在处理过首个成功 live batch 后，每约 250 ms 先推进 wall-clock
  control event，再进入下一次 Rabbit poll；该机制不等待全市场齐全。
- `SnapshotTrigger` 将 A25 置为 one-shot 后，pipeline 从内存
  `QuoteStateStore` 生成 Redis 锚点；Redis 是投影，不是快照输入权威。
- 若某个 source batch 的逻辑时刻本身跨越触发点，`EngineCore::on_batch`
  先推进 trigger、随后处理整批 ticks，而 pipeline 在处理后生成 Redis 命令；
  本 release 只对 09:32:10 有显式 batch split，不能推断 09:25:06 具有同样
  的队列内屏障。真实 freeze 仍代表当时已处理状态，不代表 TD 时间排序的
  历史可见集合。

## 发现并修复的开发分支故障路径

静态检查发现：冻结 A20/A24/A25 trigger 在外部提交前已被标成 emitted；若
Redis 命令失败，后续 `on_clock()` 不会再次生成该锚点。失败会进入运行错误路径，
但锚点本身没有重试命令。

t1-v2 开发分支新增进程内 pending Redis snapshot 命令：冻结的 A20/A24/A25
投影在 Redis executor 成功前保持原命令字节，后续 tick 不会改写冻结内容；Redis
成功后清除 pending。该机制不是 durable outbox，进程崩溃会丢失 pending；TD
snapshot statement 的独立重试/跨 Redis-TD 双写一致性也不在本修复范围。

提交：

```text
repo: stock-situation-runtime
branch: codex/task-q2-pure-function
commit: f4c3eb50056d7d1faa4cd1bcfe2fd7ce71da6e51
status: local only; not pushed, merged, or deployed
```

## 测试与副作用

- 新故障注入先在旧逻辑上 RED：Redis 首次失败后没有重发 A25 锚点。
- 新逻辑 GREEN：故障后仍处理后续 tick；下一次 Redis attempt 重发同 key、同
  payload 的冻结锚点；成功提交后不会第三次重复发出。
- t1-v2 全依赖构建和完整 C++ self-test：PASS，产物在
  `/home/exedev/validation/t1v2-a25-redis-retry-final/t1_v2_test`。
- Core 回归：`691 passed, 3 warnings`；`compileall` PASS；两个 repo 的
  `git diff --check` PASS。
- 新故障测试使用 fake Redis 与 Null TD，不连接数据服务。未运行真实 TD replay
  或真实 Redis 写入；因此这次修复的故障分支证明来自确定性故障注入，既有真实
  replay parity 证据没有被重新覆盖。

## 阶段对齐与后续

Phase A 已确认当前 release 的 Rabbit schema、consumer batch 边界、t1-v2
`TickBatch` metadata 丢失、TD 投影差异、09:25 live control-clock 顺序和 Redis
失败重试边界。尚未知：真实 publisher 对 outer/inner `batch_id` 的定义与一致性、
每个 delivery 的成员/到达顺序、`sent_at` 的具体业务来源、`p` 的意义、历史
`available_at`。

本审计当时建议的独立、开发态 batch metadata contract 已在后续 t1-v2 开发提交
`9a52e8af5f8c8adcc79151abf8dcd35c474c35ff` 完成：保留 outer 与 inner metadata，
不把进程本地 `seq_no` 冒充 producer sequence/arrival，也不修改 live
consumer/ACK/schema。验证命令：

```text
bash make.sh --full --self-test --out=/tmp/t1v2-rabbit-batch-metadata-final4
result: build PASS; t1_v2 self-test passed
git diff --check: PASS
```

该提交仅存在于 `stock-situation-runtime` 本地分支
`codex/task-q2-pure-function`，未推送、合并或部署。测试走 protobuf
serialize→parse→adapter 路径及合成 wire-header fixture；没有消费真实 Rabbit，
也没有访问/写入 Redis 或 TD。sidecar 不参与 Engine/Q2 计算。仍不能证明真实
delivery 的成员/到达顺序、outer/inner ID 的业务关系、`sent_at` 的来源/单位、
逐 tick arrival/历史 `available_at` 或 `p` 的语义；这些仍为 UNKNOWN。

因此这关闭的是开发态 metadata propagation，不是 Rabbit live parity、TASK-008、
Phase P 或全流程。TD 3 秒 frame 与 Rabbit delivery 的映射仍须以真实来源证据
为依据，不得从 metadata sidecar 推断。
