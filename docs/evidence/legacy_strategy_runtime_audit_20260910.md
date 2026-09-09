# Legacy auction strategy runtime audit

日期：2026-09-10（Asia/Shanghai）  
主机：`cobra-ion`

## 生产运行入口

当前 systemd 服务实际执行：

```text
t1-v2-live.service
→ /home/exedev/services/t1-v2/current/content/bin/t1_v2
→ C/t1_v2/main.cpp
→ RuntimeLoop / TickSourceFactory / RedisCommandExecutor / TDengineCommandExecutor
```

`engine-next.service` 独立运行 Python 报告/上下文链；本审计不修改这两个服务。

## 阈值规则审计

仓库中能找到旧 `C/analysis.cpp` 的：

```text
bid_amount > ask_amount * 1.5
→ “买方力量强”日志
```

但当前生产 `t1_v2` 的实际入口使用 `C/t1_v2/main.cpp` 与 `C/t1_v2` 组件；
该活动源码路径没有同一阈值实现。旧 `analysis.cpp`/`C/t1.cpp` 代码不能仅凭存在
于发布源目录就被视为当前 live consumer 的策略 oracle。

因此该规则当前状态为：

```text
legacy source evidence       = OBSERVED
current live consumer proof   = NOT_PROVEN
unit/phase/lifecycle oracle   = NOT_PROVEN
engine_core migration status  = UNKNOWN / blocked
```

不把它迁移成 `AuctionShadowStrategy`，也不产生 `TURN_STRONG` 或 `BUY` 结论。

## engine_next 规则状态

`engine_next/strategy_skill_layer/auction_plate_buckets.py` 包含
`_is_turn_strong()`、主题 bucket 和期望分类，但当前测试主要使用合成
`StockStateSnapshot`；尚无同一真实日期、同一 Redis/TD 输入、同一旧 consumer
状态生命周期的 differential oracle。因此这些仍属于 `UNKNOWN`，不能作为第一条
正式迁移策略。

## 当前可迁移对象

已闭环且已接入真实 TD/Redis 旁路的仍是事实级对象：

```text
P / M / RB / RA
→ 相邻 Segment 变化
→ directional pressure（盘口代理）
→ FACT_ONLY / OBSERVE
```

它们可以继续作为后续策略的输入，但不是策略结论本身。

## 下一步门槛

只有同时取得以下证据，才允许新增第一条 Auction Shadow Strategy：

```text
当前 engine_next/t1-v2 consumer 真实调用位置
真实字段与单位
触发时间和状态生命周期
至少一组真实正例与反例
PARTIAL/MISSING 行为
legacy differential oracle
```

在此之前继续扩策略框架属于越界；应优先审计 engine_next 的实际读取链或等待
下一个交易日捕获一个有明确 consumer 记录的最小规则。
