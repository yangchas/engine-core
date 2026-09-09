# Real Redis auction projection → engine_core fact shadow

日期：2026-09-10（Asia/Shanghai）  
数据交易日：2026-09-09  
主机：`cobra-ion`  
输入：生产 loopback Redis `market:auction:20260909:{0920,0924,0925}` 的 Top-200
投影（只读）

## 验证身份

- engine_core commit：`0467c4c76f85200333ae3d25585ecd5fd9d7821e`
- 上传归档：`engine-core-0467c4c.tar`
- 归档 SHA-256：`8C5A6765C281F4AF000B9EA89AD39F0B63ED37873B41001359CEA92DDCCA3BA4`
- 隔离目录：`/home/exedev/validation/engine-core-0467c4c`
- Linux 测试：`168 passed`
- `compileall`：PASS

## 输入与安全边界

从 Redis 读取 0920/0924/0925 三个 `top_amount` JSON，挑选
`600519`，保留每个快照 `meta.ts` 作为 source record time。然后将字段映射为
现有事实函数需要的 canonical endpoint state：

```text
price              → price_milli
auction_amount     → auction_amount_yuan
bid_amount         → auction_bid_amount_yuan
ask_amount         → MISSING（Redis 当前载荷没有该字段）
```

调用的是现有 `build_segment_frame()` 与 `build_auction_fact_shadow()`，没有启动
Engine，也没有 Redis/TD 写入、Rabbit ACK、repair、网络 fallback、通知或 effect。

## 业务锚点与结果

```text
0920 source_record_time = 09:20:03.083
0924 source_record_time = 09:24:10.110
0925 source_record_time = 09:25:06.081

business anchors = 09:20:00 / 09:24:00 / 09:25:00
```

shadow 输出：

```text
price_delta_milli   = 10
amount_delta_yuan   = 11,875,800
rest_bid_delta_yuan = -4,959,000
rest_ask_delta_yuan = None
pressure_delta_yuan = None

price      = STABLE
amount     = VOLUME_EXPANDING
order_book = PRESSURE_UNKNOWN
breadth    = BREADTH_UNAVAILABLE
theme      = THEME_UNAVAILABLE
status     = PARTIAL
state      = OBSERVE
decision   = FACT_ONLY
```

哈希：

```text
comparison_hash = 6a246961e4381df053eb73b5228c0a272a4eb572237c126eff7da2466c5b8ae5
content_hash    = ffecd3f1cd847b0b01f6e225f989b4f7dd7cd258ce3223375586995dd07f9adb
evidence_hash   = 8f42addd2edfed8b09df5a058bb3aabb800ed3bad59486ea1b0f10a1c64d789e
```

## 结论

该结果证明：

```text
真实 Redis auction Top-200 projection
→ source-specific normalization
→ engine_core Segment/Comparison fact
```

能够稳定运行，并且不会把 Redis 缺失的卖方金额伪装为零。它不证明：

```text
Redis 0920/0924 是全量 authority
Redis Anchor 当前包含 ask_amount
Redis 与 TD writer 具有同一 Rabbit batch 因果顺序
旧 engine_next loader 的 redis_keys_written 字段语义正确
```

因此该旁路是 `ENGINE_CORE_REDIS_FACT = PASS（单标的、Top-200、共享字段）`，
不是生产链整体通过，也不触发对 producer 或旧 loader 的在线修改。
