# Auction fact shadow：600519 09:20 → 09:24

日期：2026-09-07（Asia/Shanghai）  
分支：`codex/feature-engine-integration`

## 边界

本次只实现 Gate B 已证明的事实级 Shadow，不迁移旧系统的“买盘强”、
“转强/转弱”、撤单或波动阈值。旧 consumer、单位、状态生命周期和正式输出
仍未形成闭环 oracle，因此不产生 `BUY`、`EV`、`attack` 或其他交易结论。

代码入口：`src/engine_core/auction_shadow.py`  
测试：`tests/test_auction_shadow.py`

## 输入与输出

输入必须是同一标的、相邻、非重叠的 `SegmentFrame`。输出
`AuctionFactShadow`，只包含：

```text
price_delta_milli
amount_delta_yuan
rest_bid_delta_yuan
rest_ask_delta_yuan
pressure_delta_yuan
```

并保留已验证的比较标签：

```text
PRICE_* / VOLUME_* / PRESSURE_* / *_UNAVAILABLE
```

`pressure` 仍是 `RB - RA` 的盘口方向压力代理，不是资金净流入。
任何缺失值保持 `None`，不使用零值补齐。

## 600519 Golden 结果

来源：`tests/fixtures/facts/auction_600519_20260903.json`。

```text
status                = PARTIAL
coverage_status       = PARTIAL
price_delta_milli     = -2,060
amount_delta_yuan     = 4,407,516
rest_bid_delta_yuan   = 648,770
rest_ask_delta_yuan  = -129,960
pressure_delta_yuan   = 778,730

price                 = PRICE_WEAKER
amount                = VOLUME_EXPANDING
order_book            = PRESSURE_IMPROVING
breadth               = BREADTH_UNAVAILABLE
theme                 = THEME_UNAVAILABLE
```

Segment A `[09:15:00,09:20:00)` 是 `PARTIAL`；Segment B
`[09:20:00,09:24:00)` 是 `READY`。业务区间仍与实际 source record time
分离，且不把端点变化解释为段内路径或 first-touch。

## 哈希与追溯

`content_hash` 只包含事实语义、质量/覆盖状态、比较标签和端点数值；
`evidence_hash` 单独包含比较证据、字段 lineage 和稳定 evidence refs。
输出 `as_trace()` 固定为：

```text
state = OBSERVE
decision_status = FACT_ONLY
```

因此本对象可用于 Shadow trace 和后续 legacy differential，但不能直接授权
策略或 effect。

## 验收

```text
python -m pytest -q
→ 93 passed locally and on cobra-ion Python 3.12.3
```

本次没有修改 Engine、Replay、Provider、Redis/TD 写入或 Rabbit ACK。
