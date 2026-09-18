# ThemeAuctionDeltaFact 轮子验证（2026-09-18）

## 范围

新增 `ThemeAuctionDeltaFactV1`，输入是已经规范化的 0924→0925 行：

```text
symbol
tag=0925
previous_tag=0924
amount_yuan
amount_delta_yuan
bid_amount_delta_yuan
change_pct_delta
amount_ratio
```

主题归属不由 Core 猜测，而由调用方显式提供：

```text
symbol -> ((theme_id, weight), ...)
```

该轮子只做加权聚合和字段质量传播，不做排名、阈值、转强/转弱、买卖或真实
资金净流入判断。

## 合同

- 数值字段必须已在 Provider/Adapter 边界完成单位和语义确认。
- 缺失字段不会变成零；某字段未覆盖全部贡献股票时，该字段结果为 `None`，主题
  状态为 `PARTIAL`。
- 所有字段完整时为 `READY`；证据 refs 进入 `evidence_hash`，不进入业务
  `content_hash`。
- 同一 symbol 重复输入、同一 symbol 重复主题权重都会拒绝。
- 只接受 `0924→0925`（参数可显式指定）的规范化行，其他锚点不会被偷偷混入。

## 验证

本地当前全量测试：`545 passed`；compileall 通过。该结果证明纯轮子边界、缺失
传播、加权结果、hash 分离和重复输入拒绝，不证明生产主题 mapping authority、
热板字段单位或旧主题 consumer 的同输入 oracle。

因此当前状态仍为：

```text
THEME_DELTA_WHEEL = VERIFIED_PURE_FUNCTION
THEME_MAPPING_AUTHORITY = UNKNOWN
LEGACY_THEME_STRATEGY_PARITY = NOT_PROVEN
PRODUCTION_USE = NOT_AUTHORIZED
```

## Cobra-ion 真实 Redis 探查

使用现有共享 Python 3.12 环境运行只读 `run_real_cache_inventory.py`，没有写 Redis、
没有读取 Rabbit、没有调用网络 fallback。结果：

```text
config:plate_mapping:s2p  = hash, 2914 fields, JSON-list values
market:stock_plate        = hash, 5955 fields, plain-string values
market:stock_reason       = hash, 2486 fields, plain-string values
config:plate_mapping:info = missing
config:plate_mapping:full_sync_info = missing
```

真实日分区也显示 `cache:hot_plates:2026-09-18` 有 50 行、
`cache:yest_limit_pool:2026-09-17` 有 47 行，但两者 metadata 均没有可验证的
`available_at_ms`/`field_units`。本次探查 artifact：

```text
/home/exedev/validation/engine-core-theme-map-20260918.json
sha256=caf8865a022417f49b55e57d2396b6ceb37b859bc4c23268356f4aa29c139d55
```

这关闭了真实 Redis 方言和覆盖规模的观察证据，但没有关闭历史 replay 可用时间、
主题字段单位或旧 consumer 同输入 parity，因此仍不授权接入生产主题策略。
