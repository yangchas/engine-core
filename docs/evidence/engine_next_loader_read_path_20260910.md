# engine_next auction loader read-path audit

日期：2026-09-10（Asia/Shanghai）  
主机：`cobra-ion`  
生产 release：`/home/exedev/services/engine-next/releases/20260903_e272842`  
审计范围：`engine_next.runtime.intraday_data_hub.IntradayDataHub.load_auction_snapshots`

## 实际执行

在生产 release 的共享 Python 3.12 环境中创建 `IntradayDataHub`，注入 loopback
Redis client，调用：

```python
hub.load_auction_snapshots("2026-09-09")
```

只读取：

```text
market:auction:20260909:0920
market:auction:20260909:0924
market:auction:20260909:0925
```

本次没有调用 `recover_auction_anchor()`，也没有 Redis/TD 写入、网络 fallback、
repair、通知或 effect。

## 实际结果

```text
dataset    = auction_snapshots
source     = redis_snapshots
row_count  = 600
```

返回 600 行是因为三个 Redis `top_amount` 载荷各自最多 200 行，并非全市场逐
标的事实。抽样标的中：

```text
600519 = 0920/0924/0925 三个 projection row 均出现
000001 = 未出现在 0920/0924 Top-200；0925 Anchor 不在本方法读取路径
000002 = 未出现在 0920/0924 Top-200；0925 Anchor 不在本方法读取路径
```

600519 的 loader 输出保留了：

```text
timestamp: 1788916803083 / 1788917050110 / 1788917106081
amount:    10831500 / 21532500 / 33408300
bid:       0 / 5089500 / 130500
ask_amount_present = false
```

这与本次 engine_core 直读 TD 及 Redis/TD 共享字段比较的真实值一致，且
0924/0925 的 loader delta 为：

```text
amount_delta     = 11875800
bid_amount_delta = -4959000
ask_amount_delta = None
```

## 发现的迁移边界

1. `load_auction_snapshots()` 是 Redis 本地读取路径，不做网络或 TD fallback，
   适合作为未来 engine_core 的只读 projection 输入，但它读取的是 Top-200
   projection，不是全量 Anchor authority。
2. `IntradayFetchResult.redis_keys_written` 在该读取方法中被填成读取过的三个
   key。实际没有写操作，但字段名会误导调用者和审计日志；新内核应分开
   `keys_read` 与 `keys_written`。当前不修改生产 release，先记录为 legacy
   contract 差异。
3. `recover_auction_anchor()` 另有明确的 `redis.set()` 回写路径；它不能被
   engine_core 只读旁路调用。
4. loader 对 `ask_amount` 通过 `0.0` 保存数值，同时另有
   `ask_amount_present=False`。新内核必须保留 Missing/Not Comparable 语义，
   不能只复制这个浮点字段。

## 结论

```text
ENGINE_NEXT_READ_PATH        = OBSERVED/PASS（bounded Redis projection）
FULL_AUCTION_UNIVERSE        = NOT_PROVEN
REDIS_WRITER_SIDE_EFFECT     = 0（本次调用）
READ_WRITE_METADATA_CONTRACT = LEGACY_MISMATCH
```

这条证据支持下一步以**纯读取、字段可比性受限**的 Redis adapter 进入
engine_core；不支持把旧 loader 直接当成全量事实 authority，也不支持在当前
阶段调用 recovery 回写路径。
