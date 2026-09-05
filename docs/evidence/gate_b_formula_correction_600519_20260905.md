# Gate B：600519 09:15 盘口金额公式修正

日期：2026-09-05（Asia/Shanghai）

## 发现

原始捕获文件 `docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json` 保存了 09:15:08 的真实 `stock_tick_v2` 行，但其中的派生字段把 `auction_bid_amount_yuan` 记录为 0。该值沿用了本地旧版 `C/t1_v2/auction_calculator.cpp` 的二档价格计算方式。

cobra-ion 当前 t1-v2 发布包 `6fb3164baab00d840886da5f056587ec32f3d86a` 的生产公式使用一级价格和二档手数：

```text
rest_bid = bp1_milli * bv2 * 100 / 1000
rest_ask = ap1_milli * av2 * 100 / 1000
```

09:15:08 原始值：

```text
bp1_milli = 1,297,540
bv2       = 4
ap1_milli = 1,297,540
av2       = 0
```

因此当前发布公式的结果是：

```text
auction_bid_amount_yuan = 519,016
auction_ask_amount_yuan = 0
```

## 修正范围

- Golden fixture 的 `pre_auction_0915.state.auction_bid_amount_yuan` 修正为 `519016`。
- 原始捕获文件保持不变，作为历史观察证据；其旧派生值不再作为当前契约 oracle。
- 09:20→09:24 相邻段的已验证数值不受影响，因为该比较的起点是 09:20 快照。
- 修正只针对当前发布包已验证公式，不推断上游供应商对字段的更深层语义。

## 证据

- 原始行：`docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json`
- 当前发布源：`cobra-ion:/home/exedev/services/t1-v2/current/content/source/C/t1_v2/auction_calculator.cpp`
- 当前源文件 SHA256：`c3190f2b32c354b6cf67adc75381e21790b86c46e136e438e52854217ccbd932`
