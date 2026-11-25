# Rabbit/TD contract audit — 2026-09-24

审计方式：ECC `contract-first`，只读检查 exact t1-v2 release 与 Core
canonical contract；未消费 Rabbit、未 ACK、未写 Redis/TD、未修改生产 C++。

## 结论

```text
RABBIT_WIRE_SCHEMA=OBSERVED
RABBIT_RAWTICK_FIELDS=OBSERVED
CORE_RABBIT_FIXTURE_CONTRACT=IMPLEMENTED
T1_V2_RUNTIME_METADATA_ALIGNMENT=PARTIAL
RABBIT_ARRIVAL_ORDER=UNKNOWN
COMPLETION_WATERMARK=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
```

证据清单：

`/home/exedev/validation/td-rabbit-rabbit-contract-20260924T224500+0800/rabbit_contract_inventory.json`

## 已确认的 Rabbit 合同

exact release 的 `schema.proto` 定义：

- `DataRecord`：`tss/lp/o/h/l/lc/a/v/p`、五档价格/数量、
  `symbol/exchange/market`；
- `DataBatch`：`batch_id`、`records[]`、`sent_at`；
- `DataRequest`：`NONE/GZIP/DEFLATE` 与 `compressed_data`。

wire 外层还携带 `proto_version/compression/batch_id/record_count/原始与压缩
大小/timestamp`。GZIP/DEFLATE 是传输压缩，不是回放时间切片；回放仍按
TD 的 3 秒半开窗口产生输入。

## 当前实现事实

1. `RabbitMqBatchDecoder` 能解析 wire header 和 protobuf records，但返回的
   C++ `TickBatch` 只有：

   ```text
   mode, logical_ts_ms, wall_ts_ms, seq_no, ticks[]
   ```

2. wire `timestamp` 只被写入 `TickBatch.wall_ts_ms`；`batch_id`、
   `DataBatch.sent_at`、`record_count` 没有传播到 `TickBatch`。

3. `seq_no` 是本次 Rabbit consumer session 的本地序号，不是 delivery
   sequence；`delivery_tag` 只在 `TickSourceResult` 中存在。

4. `DataRecord` 没有每条 tick arrival timestamp、delivery sequence、
   completion/watermark 或 historical `available_at`。

5. `RabbitFixtureAdapter` 已按 Core 合同保留 `DataBatch.batch_id`，并按字段
   处理 proto3 default ambiguity；但它还没有与 exact t1-v2 C++ `TickBatch`
   做生产路径级接线，这属于后续经批准的跨仓库合同任务。

## 对原计划的影响

这不推翻 Rabbit-primary 方向，反而明确了边界：

```text
Rabbit DataRecord/DataBatch
    → RawTick/TickBatch
    → t1-v2 Q2/auction
```

TD 回放可以复用同一 `RawTick` 字段和 t1-v2 计算，但不能凭 TD 推断
Rabbit 的 delivery membership、arrival order 或 historical `available_at`。
当前 Phase N 的真实 `TD → t1-v2 → isolated Redis → Core` 重复确定性仍然
有效，但不能升级为 Rabbit equivalence 或 NORMAL opening。

## 下一项合同任务（不自动实施）

在不改生产 consumer/ACK 的前提下，下一项应先形成跨仓库合同审查：

- 是否将 `batch_id/sent_at/record_count` 作为只读 evidence 传播到统一
  batch metadata；
- 如何明确 `seq_no`、delivery tag、source sequence 的不同语义；
- 是否需要真实 Rabbit capture 才能证明 arrival/completion；
- Core 与 t1-v2 两侧 golden fixture 如何由同一 protobuf serialize→parse
  产出。

未经明确批准，不修改 `schema.proto`、Rabbit consumer、ACK 或生产服务。
