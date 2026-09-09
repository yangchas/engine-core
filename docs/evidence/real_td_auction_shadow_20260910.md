# Real TD auction fact shadow — 600519

日期：2026-09-10（Asia/Shanghai）  
数据交易日：2026-09-09  
主机：`cobra-ion`  
代码分支：`codex/feature-session-engine-integration`

## 验证身份

- engine_core commit：`36ef854207f4b2ba74667acff4749829b6507310`
- 上传归档：`engine-core-36ef854.tar`
- 归档 SHA-256：`41C1C7873CDB7D4D7D0AFBD099E2C0F82F142E3E789A1BECC580D47BD52D084D`
- 隔离目录：`/home/exedev/validation/engine-core-36ef854`
- Linux Python：生产共享 venv，Python 3.12
- 服务器完整测试：`161 passed in 1.00s`
- `compileall`：PASS
- 结果文件：`/home/exedev/validation/engine-core-36ef854/real-auction-shadow-20260909-600519.json`
- 结果 SHA-256：`f64427ec51c1f8167cff850f04ca12090d20ccf7c6ee07208cf1376b5792dd9a`

## 数据与访问边界

脚本通过生产环境已经存在的 `taos` 客户端，对 TDengine
`market_data1.auction_snapshot_v2` 执行只读 `SELECT`。查询固定使用交易日
`2026-09-09`、标的 `600519` 和 `0920/0924/0925` 三个 `auction_tag`。

本次没有：

- 新建 Rabbit consumer 或改变 ACK；
- 写入 Redis 或 TDengine；
- 执行 repair/backfill/recovery；
- 发送通知、邮件或其他 effect；
- 修改生产服务或其 current release。

## 真实行与业务锚点

| tag | source record time | business anchor | px_milli | match_amt_yuan | rest_bid_amt_yuan | rest_ask_amt_yuan |
|---|---|---|---:|---:|---:|---:|
| 0920 | 09:20:03.083 | 09:20:00 | 1,305,000 | 10,831,500 | 0 | 2,740,500 |
| 0924 | 09:24:10.110 | 09:24:00 | 1,305,000 | 21,532,500 | 5,089,500 | 0 |
| 0925 | 09:25:06.081 | 09:25:00 | 1,305,010 | 33,408,300 | 130,500 | 130,501 |

`source_record_time` 原样保留；业务锚点没有被延迟的 TD 写入时间替代。
本样本证明的是 TD 投影行的事实，不证明 Rabbit arrival、原始 batch 成员关系或
SnapshotTrigger 的因果顺序。

## Segment 结果

运行时按业务半开区间构造：

```text
A = [09:20:00, 09:24:00)
B = [09:24:00, 09:25:00)
```

两个段的 source observation range 分别为：

```text
A: 09:20:03.083 → 09:24:10.110
B: 09:24:10.110 → 09:25:06.081
```

本次 shadow 输出：

```text
Segment A semantic/content hash = fa78051f6078cf2cafb4df479897847f6c9ba02d94705a54519175ef51e55b96
Segment B semantic/content hash = a29b095bd237561ae4862a6d11d487accfe5321c04617e89ab54b42ab4be5c73
Comparison semantic hash         = da98e73244205a742d53c5a7f198721567378b67ff90717a8b056ae2fa51a89b
Comparison content hash           = 4206e5cdbe6697a463bb48e1d0201b7c5111a7d13c3a6d8529e2e2066f5b83da
Comparison evidence hash          = d3844fc5eefbccf4d774180d276376ed2d1e67a16645b49bc6dcb43c668248bc
```

端点变化：

```text
price_delta_milli     = 10
amount_delta_yuan     = 11,875,800
rest_bid_delta_yuan   = -4,959,000
rest_ask_delta_yuan   = 130,501
pressure_delta_yuan   = -5,089,501
```

标签：

```text
price      = STABLE
amount     = VOLUME_EXPANDING
order_book = PRESSURE_WEAKENING
breadth    = BREADTH_UNAVAILABLE
theme      = THEME_UNAVAILABLE
state      = OBSERVE
decision   = FACT_ONLY
status     = PARTIAL
```

`pressure = rest_bid - rest_ask` 是盘口方向压力代理，不是权威资金净流入。
由于本次查询只包含单一标的，没有市场宽度和主题集合，`PARTIAL`/`UNAVAILABLE`
是诚实结果，不做零值补齐，也不产生 `BUY`、`EV` 或其他策略结论。

## 迁移判断

本次关闭了一个真实的只读业务事实切片：

```text
真实 TD auction_snapshot_v2
→ 统一业务锚点与 source time
→ Segment A/B
→ adjacent comparison
→ fact-only shadow trace
```

仍未关闭的链路边界：

```text
Rabbit/Gateway → t1-v2 batch membership
t1-v2 AuctionState → Redis/TD 两 writer 同状态证明
engine_next loader → report 的读取链
正式 AuctionShadow strategy 规则
```

下一步应先做小范围 Redis/TD projection 可比性和现有 `engine_next` 读取链审计，
只有找到已验证的旧规则，才迁移第一条 `AuctionShadowStrategy`。本证据不授权
修改 producer，也不代表整个生产链已经 PASS。
