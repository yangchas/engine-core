# engine_next context → fact read-boundary audit

日期：2026-09-10（Asia/Shanghai）  
主机：`cobra-ion`  
生产 release：`/home/exedev/services/engine-next/releases/20260903_e272842`

## 目的与范围

本次只读审计验证旧 `engine_next` 的真实上下文组装链：

```text
IntradayContextRequest
→ IntradayContextBuilder
→ quote/cache/auction rows
→ StockStateSnapshot / IntradayContext
```

范围限定为少量标的 `600519`、`000001`、`000002` 和
`RunPhase.POSTMARKET`，不调用 Rabbit consumer，不调用
`recover_auction_anchor()`，不执行 Redis/TD 写入，不执行网络补齐、通知或
effect。为保证审计路径不产生副作用，已对已知写入点和网络名称 fallback
设置 loopback guard；这证明本次调用没有写入，不证明旧 builder 原生实现本身
是 side-effect-free。

## 真实结果

```text
snapshot_count       = 3
auction_count        = 3
session_fact_count   = 2
quote/cache/auction  = 3 / 3 / 3
read_only            = true (guard 未捕获写入)
names                = 空（本次禁用 F10 fallback）
```

抽样结果保留了旧读取链的真实字段：

```text
600519  plate=国有企业  auction_amount=33408300  current_pct=-0.014
000001  plate=银行      auction_amount=2187400   current_pct=-0.014
000002  plate=地产链    auction_amount=2702900   current_pct=-0.014
```

## 发现 1：旧 freshness 路径会把未来 source timestamp 当作新鲜

审计时墙钟约为当日 `09:26`，但生产 Q2 quote 的 source timestamp 为
`15:00:03`。旧 `_summarize_quote_freshness()` 使用：

```python
age_ms = max(now_ms - quote_ts_ms, 0)
```

因此未来 source timestamp 被静默截成 `age_ms=0`，结果显示：

```text
latest_quote_age_seconds = 0
quote_freshness           = 3/3
```

这属于旧读取路径的时间安全缺陷：未来数据没有被拒绝，反而被报告为新鲜。
`engine_core` 的目标契约仍是显式 `max_future_skew_ms` 失败关闭；本次没有修改
生产 `engine_next`，该问题只作为迁移边界记录。

## 发现 2：builder 不是天然只读边界

旧 `IntradayContextBuilder` 的默认路径包含以下潜在副作用：

```text
prime_runtime_state()
→ _ensure_hot_rank_cache()
→ 可能调用 fetch_hot_rank()

build_from_primed()
→ _write_cached_session_facts()
→ 可能写 Redis
→ SectorFlowTracker.update_and_evaluate()
→ 可能写 zset/expire

_load_fallback_stock_names()
→ F10 网络请求

_load_auction_rows()
→ AUCTION/INTRADAY 可能进入 recover_auction_anchor()
```

所以 engine_core 旁路不能直接把整个 `IntradayContextBuilder` 当作只读
Provider。正确迁移边界是：抽取已验证的读取/字段转换部分，外部显式控制
网络、恢复和写入能力，并向核心只提交冻结的输入数据。

## 迁移结论

```text
LEGACY_CONTEXT_READ_RESULT       = OBSERVED
BOUNDED_NO_WRITE_AUDIT           = PASS (本次 guard 范围内)
BUILDER_NATIVE_SIDE_EFFECT_FREE  = NOT_PROVEN
FUTURE_SOURCE_TIMESTAMP_SAFE     = FAIL (旧路径)
CORE_Q2_FUTURE_REJECTION         = TARGET CONTRACT
FULL_UNIVERSE_AUTHORITY           = NOT_PROVEN
```

不得据此宣称旧系统已完成策略迁移，也不得在本分支修复生产 builder。下一步
只允许把需要的字段读取边界做成 engine_core 的薄、只读输入转换，并为第一条
已由 legacy consumer 证据证明的规则建立 differential fixture；在取得
consumer oracle 之前不实现正式 AuctionStrategy。

## 证据限制

本次没有新增 Rabbit consumer 或 runtime audit hook，因此无法证明：

```text
Rabbit batch membership
Gateway decode/drop counters
trigger 与 writer 的同批因果
```

这些保持 `UNKNOWN`，不使用 TD 或 Redis projection 反推。

## Core verification identity

本证据随 engine_core commit `24aef250cf9974bd2dac7c8d62344945fca4ec17` 固化。

```text
archive SHA-256 = 61402a106db0f4f3d33fc5e66727aaddf3d8180176fc7726efa886f80f43041a
remote isolated path = /home/exedev/validation/engine-core-24aef25
server Python = 3.12
pytest = 168 passed in 1.02s
compileall = PASS
```

远端验证使用归档副本，不覆盖旧验证目录，不触碰生产服务或生产数据。
