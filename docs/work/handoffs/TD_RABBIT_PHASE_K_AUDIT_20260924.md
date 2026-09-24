# TD/Rabbit global replay Phase K 09:15 baseline validation

审计时间：2026-09-24 20:27–20:30 +08:00<br>
交易日：2026-09-23<br>
源窗口：`[09:15:00,09:25:09)` Asia/Shanghai

## 结论

```text
REAL_TD_TO_T1V2=PASS
ISOLATED_REDIS_WRITE=PASS
REDIS_REPEAT_SEMANTIC_DETERMINISM=PASS
CORE_READS_REAL_REDIS=PASS
SNAPSHOT_SYMBOL_SET_PARITY=PASS
AUCTION_ANCHOR_FIELD_PARITY=PARTIAL
PHASE_K_OVERALL=PARTIAL
```

本阶段验证了一个关键输入边界：回放若从 09:20 开始，会遗漏只在
09:15 写入基线、之后没有新 tick 的股票；完整竞价回放必须从 09:15
基线开始。该结论来自真实 TD 与真实 t1-v2 Redis 写入，不是合成 fixture。

## 真实链路

使用当前发布包：

```text
/home/exedev/services/t1-v2/current/content/bin/t1_v2
```

输入为 TD `market_data1.stock_tick_v2`，回放写入 Redis DB5、前缀
`task009k:`，TD 写入显式关闭：

```text
2026-09-23 09:15:00 <= ts < 09:25:09
REPLAY_WRITE_REDIS=true
REPLAY_WRITE_TDENGINE=false
```

运行输出：

```text
source_in=212022
source_reject=0
batches=604
ticks=212022
clocks=1
redis_cmds=428284
redis_committed=212022
td_sql=0
ack=0
```

Redis DB5 生成 5222 个逐股 Q2 hash，以及 0920、0924、0925、latest、
anchor 和 runtime 投影。

## 独立重复

同一窗口再次使用 Redis DB4、前缀 `task009k2:`。两次均为 5233 个
隔离 key；去除前缀差异并排除 `runtime.redis_bytes` 后：

```text
missing=[]
extra=[]
value_mismatch_count=0
normalized_sha_1=44424eee6fd2b0f914b6ab2420a7d52b1d80480beda80ccb56005354a68d55eb
normalized_sha_2=44424eee6fd2b0f914b6ab2420a7d52b1d80480beda80ccb56005354a68d55eb
semantic_equal=true
```

## 09:15 基线事实

与 0925 `auction_snapshot_v2` 做真实集合核对：

```text
TD 源 tick 行数                  212022
TD 源股票数                      5222
t1-v2 Q2 股票数                 5222
0925 快照行数/股票数             5222
snapshot_missing_q2              []
q2_extra_vs_snapshot             []
```

对照 Phase J 的 `09:20–09:25:09` 结果，原先缺少的 16 只股票在 09:15
基线回放后全部进入 Q2。它们在真实 TD 中只有 09:15 单条记录或极少的
早盘记录；因此不能把 09:20 起始回放的集合当作全市场 0925 集合。

## Core 真实 Redis 读回

从 DB5 读取 5222 个真实 Q2 hash 并冻结 capture，交给 Core：

```text
row_count=5222
coverage=1.0
ordered/shuffled projection hash equal=true
sample engine hashes equal=true
replay_status=REPLAY_PARTIAL
normal_opening_pass=UNPROVEN
```

Core 报告 `stale_count=17`，因为少数股票只有 09:15 基线，在 09:25
按 10 秒 freshness policy 已过期；这被保留为 `PARTIAL`，没有将 stale
伪装成当前行情，也没有用零填充。另一个真实事实是 t1-v2 0925 A2
元数据 `n=5208`，而逐股 Q2 为 5222。进一步逐股核对发现，A2/anchor
缺少的 14 个 Q2 symbol 在旧 `auction_snapshot_v2` 中全部是
`px/chg=NULL` 且 `match/rest=0` 的不可用事实行；因此这 14 个不应被
当成 Q2 丢失。A2 数值字段与旧快照的完整同口径 parity 仍未完成，保持
`PARTIAL`。详细清单见 `a2_missing_symbol_audit.json`。

对剩余 5208 个 A2/anchor symbol 做了真实逐字段核对：

```text
chg_bp                         5208/5208 一致
match_amt_yuan                5208/5208 一致
rest_bid_amt_yuan             5208/5208 一致
rest_ask_amt_yuan             5208/5208 一致
price（可比较的非 NULL 行）  5068/5068 一致
price 为 NULL 的行             140
```

这 140 行的旧快照价格为 `NULL`，t1-v2 Q2 的对应 `a25` 为 `0`；其余
成交额和买卖未匹配额仍逐项一致。按照 `Missing != Zero`，这 140 行不
被伪报为严格 price parity，因此整体锚点字段结论保留
`PARTIAL_WITH_EXPLICIT_UNAVAILABLE`，而不是强行升级为全量 PASS。完整
结果见 `a2_field_parity_audit.json`。

## 副作用边界

- Redis 只写 DB5/DB4 唯一测试前缀；未写 DB0。
- TD 写入关闭，`td_sql=0`。
- Rabbit/ACK 未访问，`ack=0`。
- 未重启服务，未修改 consumer、ACK、生产目录或 TD retention。

## 未关闭项

```text
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
AUCTION_ANCHOR_FIELD_PARITY=PARTIAL
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
```

下一步应沿用 09:15 基线边界，单独核对 A2/0925 锚点的字段、候选过滤和
stale 处理；不能把 `n=5208` 直接解释成 Q2 缺失，也不能用旧快照反向
补造 Rabbit 到达顺序。

## 证据目录

```text
/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202721825+0800/
/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202849702+0800-repeat/
```

主要文件：`t1_v2_stdout.txt`、`redis_after.json`、
`redis_repeat_comparison.json`、`redis_q2_capture.json`、
`core_opening_from_real_redis.json`、`symbol_set_audit.json`、
`a2_runtime_summary.json`、`sha256sums.txt`。
