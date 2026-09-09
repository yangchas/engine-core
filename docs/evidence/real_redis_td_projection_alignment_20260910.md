# Real Redis ↔ TD projection alignment addendum

日期：2026-09-10（Asia/Shanghai）  
数据交易日：2026-09-09  
主机：`cobra-ion`

## Exact verification identity

- engine_core commit：`83f77622b9e84628293ad0038ba2fc6089a14420`
- archive：`engine-core-83f7762.tar`
- archive SHA-256：`EA8680D4E6F554FBFA917708B5DF63A867CF6444ABEB838CFAB0F5056218A330`
- isolated directory：`/home/exedev/validation/engine-core-83f7762`
- Linux suite：`167 passed in 1.06s`
- compileall：PASS
- result：`/home/exedev/validation/engine-core-83f7762/redis-td-auction-20260909.json`
- result SHA-256：`0b4d4321debeee645f0786da483446b44ad0af0cd2349e4585c559f7c5565453`

## Time alignment

对 `000001`、`000002`、`600519` 的 0920/0924/0925 TD 行，工具将 Redis
快照 `meta.ts` 与 TD `ts` 都规范化为毫秒时间戳后进行 as-of 对齐。

```text
timestamp_mismatch = 0
```

对应的三个时间点分别保持：

```text
0920 = 09:20:03.083
0924 = 09:24:10.110
0925 = 09:25:06.081
```

这证明当前 Redis legacy auction projection 与 TD 投影使用了相同的记录时间，
但不证明二者一定在同一个 Rabbit batch 中生成；batch 因果仍属于未观测边界。

## 字段结果

```text
row-level MATCH              = 0
row-level PARTIAL_COMPARABLE = 5
row-level NOT_COMPARABLE     = 4
row-level MISMATCH           = 0
```

在所有实际可比较的字段中：

```text
Redis amount / auction_amount_yuan ↔ TD match_amt_yuan       = MATCH
Redis bid_amount / bid_amount_yuan ↔ TD rest_bid_amt_yuan    = MATCH
Redis ask_amount / ask_amount_yuan                           = NOT_COMPARABLE
```

原因是当前 Redis 0925 Anchor 和 0920/0924 Top-200 载荷对抽样行没有稳定提供
`ask_amount`；工具没有用 `0` 补齐。`000001` 与 `000002` 不在 0920/0924
Top-200 时，同样保持 `NOT_COMPARABLE`，不把 Top-200 当成全量 authority。

## 迁移含义

当前已经具备一条真实、只读、字段受限的交叉证据：

```text
Redis legacy projection
    + Redis snapshot meta.ts
        ↕ exact timestamp alignment
TD auction_snapshot_v2
    + match/resting amount fields
```

这足以支持 engine_core 后续读取**已定义共享字段**，但尚不足以声称：

```text
Redis/TD writer 同批次同 AuctionState
Redis 0920/0924 全量逐标的 authority
Anchor ask_amount 字段已在生产稳定提供
Rabbit arrival/batch 顺序可回放
```

下一步仍应先审计 `engine_next` loader 的只读读取链，再用已验证的旧 consumer
规则选择第一条 Auction Shadow Strategy；本证据不触发 producer 修改或生产写入。
