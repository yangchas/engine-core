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
