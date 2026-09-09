# Real Redis ↔ TD auction projection comparison

日期：2026-09-10（Asia/Shanghai）  
数据交易日：2026-09-09  
主机：`cobra-ion`  
代码分支：`codex/feature-session-engine-integration`

## 验证身份

- engine_core commit：`c14cef80e27e2e4342fa45e4a20472bc411f6573`
- 上传归档：`engine-core-c14cef8.tar`
- 归档 SHA-256：`C6849F5FCCE6D9A44FE30C0817FF541D9CC0851BAD23A982A1B800E19820F177`
- 隔离目录：`/home/exedev/validation/engine-core-c14cef8`
- Linux 完整测试：`167 passed in 1.07s`
- `compileall`：PASS
- 结果文件：`/home/exedev/validation/engine-core-c14cef8/redis-td-auction-20260909.json`
- 结果 SHA-256：`f251d8735d78a9532a73a1b2a81a4b45a8d6fd2e8e1bffbda94eb4209224877c`

## 访问边界

比较工具复用服务器已有运行环境中的 `redis` 与 `taos` 客户端：

```text
Redis: HGETALL market:auction:{date}:{0920,0924,0925}
       GET    market:auction:anchor:{date}
TD:    SELECT auction_snapshot_v2
```

没有新建 Rabbit consumer，没有 ACK；没有 Redis/TD 写入，没有 repair/backfill、
通知、邮件或 effect。工具自身只读，结果在独立 validation 目录生成。

## 比较合同

TD `auction_snapshot_v2` 是 P/M/RB/RA 的逐标的投影 authority。Redis 旧投影的
字段不是同一个 schema：

```text
Redis anchor 0925:
    amount      ↔ TD match_amt_yuan
    bid_amount  ↔ TD rest_bid_amt_yuan
    ask_amount  仅在实际字段存在时比较

Redis top_amount:
    auction_amount_yuan ↔ TD match_amt_yuan
    bid_amount_yuan     ↔ TD rest_bid_amt_yuan
    ask_amount_yuan     仅在实际字段存在时比较
```

缺少 Redis 对应字段记为 `NOT_COMPARABLE`，不当作零值，也不当作 mismatch。
Redis 0920/0924 的 `top_amount` 只是 Top-200 投影，未出现的标的不被推断为
Redis 缺失事实；0925 Anchor 是独立的全量投影来源。

## 实际结果

抽样标的：`000001`、`000002`、`600519`；每个标的查询 TD 的 0920/0924/0925。

```text
row-level MATCH              = 0
row-level PARTIAL_COMPARABLE = 5
row-level NOT_COMPARABLE     = 4
row-level MISMATCH           = 0
```

`row-level MATCH=0` 的含义是“该行的三个比较字段全部具备且全部相等”；
它不表示共享字段不一致。实际字段级结果为：

```text
match_amt_yuan / amount             全部可比较样本 MATCH
rest_bid_amt_yuan / bid_amount      全部可比较样本 MATCH
rest_ask_amt_yuan / ask_amount      Redis 当前投影均未提供，NOT_COMPARABLE
```

具体观察：

- `600519` 的 Redis Top-200 0920/0924 与 TD 的匹配金额、买方未匹配金额一致；
- 三个标的的 Redis 0925 Anchor 与 TD 的匹配金额、买方未匹配金额一致；
- 当前生产 Anchor 载荷对抽样标的没有 `ask_amount`，因此不能宣称卖方未匹配量
  的跨源一致性；
- `000001`/`000002` 不在 0920/0924 Top-200 时，结果保持
  `NOT_COMPARABLE`，不把 Top-200 投影冒充全量快照。

## 结论与边界

本次关闭了：

```text
Redis 0925 Anchor amount/bid
与 TD auction_snapshot_v2 match/rest_bid
在三个抽样标的上的字段级一致性
```

本次没有关闭：

```text
Redis 0920/0924 全量逐标的投影
Redis Anchor ask_amount 的生产字段能力
Redis/TD 是否由同一 AuctionState 批次同时写出
Rabbit/Gateway batch membership 与 arrival ordering
engine_next loader 到报告的完整读取链
```

因此该结果是 `STORAGE_SHARED_FIELDS = PASS（有限字段）`，不是整个生产链
`JOINT = PASS`，也不授权修改 producer。下一步应以 `engine_next` 的只读 loader
trace 和已验证旧规则为入口，迁移第一条 Auction Shadow Strategy；对不可比字段
继续保持 `UNKNOWN/NOT_COMPARABLE`。
