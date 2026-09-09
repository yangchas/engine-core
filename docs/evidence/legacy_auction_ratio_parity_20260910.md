# Legacy auction change-ratio wheel parity

日期：2026-09-10（Asia/Shanghai）  
旧实现：`engine_next.runtime.intraday_data_hub.normalize_auction_pct_ratio`  
新轮子：`engine_core.auction.normalize_auction_change_ratio`

## 对照范围

旧实现对有限数值使用三段比例公式：

```text
|value| <= 0.35  → ratio 原值
|value| <= 30    → value / 100
otherwise       → value / 10000
```

新轮子对所有有限数值保持该公式。已覆盖：

```text
0.0997  → 0.0997
9.97    → 0.0997
997     → 0.0997
-9.97   → -0.0997
```

## 有意差异

旧实现对缺失、非法或非有限输入使用 `0.0` 默认值。新 core 遵循
`Missing != Zero`，对以下输入返回 `None`：

```text
None / 空字符串 / 非数字 / bool / NaN / ±Inf
```

```text
parity_status = INTENTIONAL_CHANGE
reason = preserve unknown instead of fabricating factual zero
```

这项差异只改变错误输入的状态表达，不改变已验证有限数值的归一化公式。
该函数目前仍是独立轮子，尚未替换 engine_next 生产调用。

## 验证

本地和 cobra-ion 均执行参数化单测，覆盖有效 ratio、百分比、bps、负值、
缺失和非法值。生产旧函数通过指定 release 的只读导入进行现场向量对照；
未修改生产服务或数据。
