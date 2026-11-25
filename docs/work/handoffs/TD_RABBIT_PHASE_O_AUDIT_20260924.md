# TD/Rabbit global replay — Phase O audit (2026-09-24)

审计时间：2026-09-24 23:14（Asia/Shanghai）<br>
状态：`PHASE_O_PARTIAL`

## 目的

在不考虑回放性能门槛、允许隔离 Redis 写入的前提下，用同一交易日的真实
TD 数据跑完 `09:15:00–09:40:00`，通过当前精确 t1-v2 发布包生成 Q2/竞价
输出，再由 Core 只读读回。TD 仍只读，Rabbit 不消费、不 ACK，生产 DB0
与生产 key 前缀不触碰。

## 输入与运行

- TD：`market_data1.stock_tick_v2`
- 交易日：`2026-09-24`
- 半开窗口：`[09:15:00, 09:40:00)`
- t1-v2 二进制 SHA-256：
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`
- Redis：DB `9`，前缀 `task009o:`
- `REPLAY_WRITE_REDIS=true`，`REPLAY_WRITE_TDENGINE=false`

独立 TD `COUNT(*)` 为 `1,194,572`，与 t1-v2 `source_in`、`ticks` 完全相等。

## t1-v2 结果

```text
batches=1224
source_in=1194572
source_reject=0
ticks=1194572
clocks=78
redis_cmds=2408279
td_sql=0
ack=0
quote_state_committed=1200751
max_batch_ticks=3648
```

## Redis/Core 读回

- DB9 `task009o:` 前缀共 5,233 个 key；Q2 active set/hash 为 `5222/5222`。
- Core 只读执行 `SMEMBERS=1`、`HGETALL=5222`，coverage `1.0`，无 missing。
- 在显式 10 秒 freshness policy、观察时刻 `09:40:00` 下，116 个 quote
  stale；projection 状态为 `PARTIAL`，不是 READY。
- Core projection hash：
  `c1428e3771fdfd07e2fe7d68e2fdac11c86960a87cce3d87596fad34359b9a1b`。
- ordered/shuffled projection hash 相等；4 个抽样 Engine hash 全相等。
- `normal_opening_pass=UNPROVEN`，`replay_status=REPLAY_PARTIAL`，事实层为
  `FACT_ONLY/OBSERVE`。

## 竞价冻结与重复证据

0920/0924/0925 的稳定三字段 legacy auction projection 与 DB1、DB6、DB7、
DB8 既有真实回放完全相等；较丰富的 market/anchor key 作为版本化输出单独
记录，不把不同 key 结构误判为 mismatch。证据见
`auction_legacy_parity.json` 和 `auction_anchor_repeat_comparison.json`。

## 副作用与限制

- Redis 生产 DB0 的 `task009o:*`：`0`；本轮只写 DB9 隔离前缀。
- TD 写入：`0`；回放日志 `td_sql=0`。
- Rabbit consume/ACK：`0`。
- `engine-next=active`、`t1-v2-live=active`；未执行重启或部署。
- Rabbit delivery membership/arrival order、completion watermark、历史
  `available_at` 和 09:25:06 的 live 可见集合仍无证据。
- 本阶段证明真实 `TD → exact t1-v2 → isolated Redis → Core` 全窗口链路，
  不证明 Rabbit 等价，也不把 `PARTIAL` 提升为 NORMAL acceptance。

## 证据目录

`/home/exedev/validation/td-rabbit-phase-o-0924-full-20260924T224351+0800/`

主要文件：`phase_o_summary.json`、`td_count.json`、`t1_v2_stdout.txt`、
`q2_capture_094000.json`、`core_opening_094000.json`、
`auction_legacy_parity.json`、`db0_isolation.json`、`runtime_status.txt`、
`sha256sums_all.txt`。
