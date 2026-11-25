# TD/Rabbit global replay — Phase N audit (2026-09-24)

审计时间：2026-09-24 22:10（Asia/Shanghai）<br>
状态：`PHASE_N_PARTIAL`

## 目的

在明确允许隔离 Redis 写入后，使用当天真实 TD 数据，通过当前 t1-v2
回放链路生成 Q2，写入隔离 Redis，再由 Core 读取并执行 09:32 opening
FACT_ONLY/OBSERVE 验证。该阶段不消费 Rabbit、不 ACK、不写 TD，也不改变
生产 DB0。

## 实际执行

验证目录：

`/home/exedev/validation/td-rabbit-phase-n-0924-0915-0932-20260924T221500+0800/`

输入与窗口：

- TD：`market_data1.stock_tick_v2`
- 交易日：`2026-09-24`
- 半开窗口：`[09:15:00, 09:32:09)`
- t1-v2 二进制 SHA-256：`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`
- Redis：DB `7`，前缀 `task009n:`
- TD 写入：`false`
- Rabbit ACK：`0`

t1-v2 最终计数：

```text
batches=753
source_in=429392
source_reject=0
ticks=429392
clocks=78
redis_cmds=878448
td_sql=0
ack=0
quote_state_committed=436777
max_batch_ticks=3648
```

独立 TD `COUNT(*)` 与 `source_in/ticks` 均为 `429392`。

## Redis 与 Core 结果

- 隔离 DB7 的 Q2 active set/hash：`5222/5222`，无缺失 symbol。
- Core 直接使用 `RedisQ2ProjectionAdapter` 读取 DB7：
  `SMEMBERS=1`、`HGETALL=5222`。
- 观察时刻固定为 `2026-09-24T09:32:09+08:00`，显式 freshness budget 为
  `10000ms`。
- projection：`coverage=1.0`、`stale=24`、`status=PARTIAL`、
  `consistency=BEST_EFFORT_MIXED_FRESHNESS`。
- newest Q2 source time：`09:32:08`；Core projection hash：
  `9f9753f6474ae28c8b10c844fc96d382060f9b861eead74ab5d60c593baebef4`。
- frozen capture 与 Core 的 ordered/shuffled projection hash 一致；四个抽样
  Engine hash 一致，结果为 `FACT_ONLY`/`OBSERVE`。
- `normal_opening_pass=UNPROVEN`，`replay_status=REPLAY_PARTIAL`。

该窗口同时产生了 0920/0924/0925 固定 auction key；0925 元数据为
`ts=09:25:06`、`n=5208`，后续 09:25 之后滚动 Q2 没有覆盖冻结 anchor。

## 重复回放

同一 TD 窗口使用同一 t1-v2 二进制再次运行到隔离 Redis DB8/`task009n2:`：

```text
batches=753
source_in=429392
source_reject=0
ticks=429392
clocks=78
redis_cmds=878448
td_sql=0
ack=0
quote_state_committed=436777
```

DB7 与 DB8 规范化后均为 5232 个语义 key（包含 active/auction/anchor），
差异数为 `0`，语义 SHA 均为：

`c88639da305a303221c8ea9ca900060a816551bcf0385244aadb62f854050a05`

只排除了 `m2:runtime` 的运行时计数/字节指标；生产 DB0 的 `task009n*`
仍为 `0`。重复证据见 `repeat_summary.json`、`repeat_comparison.json` 和
`repeat_sha256sums.txt`。

## 副作用审计

- DB0 `task009n:*`：`0`。
- TD 写入：`0`；回放日志 `td_sql=0`。
- Rabbit consume/ACK：`0`。
- `engine-next=active`，`t1-v2-live=active`；未执行重启或部署。
- Core 读回阶段只暴露 `SMEMBERS/HGETALL`，没有 Redis 写入。

## 未关闭事项

本阶段证明了真实 `TD → t1-v2 → 隔离 Redis → Core` 的 09:32 opening
FACT_ONLY 链路，但不能推出：

- Rabbit 的真实 delivery membership/arrival order；
- 历史 `available_at` 或真实 09:32:10 线上 cutoff；
- `NORMAL` opening acceptance；
- M3-1 或 `TD_WRITE_HEALTH`。

因此本阶段只记为 `PHASE_N_PARTIAL`，不将 TASK-008 提升为 NORMAL PASS。

## 证据文件

- `phase_n_summary.json`
- `td_count.json`
- `q2_capture_093209.json`
- `core_live_redis_093209.json`
- `core_opening_093209.json`
- `sha256sums.txt`
- `repeat_summary.json`
- `repeat_comparison.json`
- `repeat_sha256sums.txt`
