# TD/Rabbit global replay Phase F bounded audit

审计时间：2026-09-24 19:52 +08:00<br>
当前分支：`codex/feature-session-engine-integration`

## 结论

```text
REAL_TD_TO_T1V2=PASS
REAL_Q2_A2_PROJECTION=PASS_FOR_EXECUTION
CORE_READS_REAL_REDIS=PASS
REDIS_REPEAT_SEMANTIC_DETERMINISM=PASS
SNAPSHOT_FIELD_PARITY=PARTIAL
PHASE_F_OVERALL=PARTIAL
RABBIT_DELIVERY_EQUIVALENCE=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
TD_WRITE_HEALTH=UNPROVEN
```

## 真实证据

2026-09-18 `09:24:00–09:25:09`：

```text
TD ticks=46507
t1-v2 source batches=65
Q2 ticks=46507
clocks=1
redis_cmds=93480
td_sql=0
ack=0
```

只写 Redis DB12/`task009f:`，生成 0924、0925、latest、runtime、anchor 和
逐股 Q2。第二次 DB11/`task009f2:` 回放后，去除 DB/prefix 的 Q2/A2/anchor
语义内容完全一致；差异只有 runtime `redis_bytes`，由隔离前缀长度造成。

随后从 DB12 读取 5209 个逐股 Q2 hash 交给 Core opening validation：

```text
ordered/shuffled hash equal=true
projection=READY（仅本次捕获 universe）
replay_status=REPLAY_READY_BOUNDED
normal_opening_pass=UNPROVEN
```

## 真实快照字段对照

TD 0924/0925 快照各 5221 行，源时间为 `09:24:10.292` 和 `09:25:06.197`。

0925 对照结果：

```text
price: 4408/4408 一致
change_pct: 5209/5209 一致
match amount: 1576 mismatches / 5209
rest bid: 1626 mismatches / 5209
rest ask: 1621 mismatches / 5209
```

0924 Redis 仅是 TopN 投影，不是全市场快照；最终 Q2 的 `a24` 还可能被
09:24:10–09:24:20 后续 tick 更新，不能代替 09:24:10 冻结值。金额和剩余
量差异保留为字段口径/版本待核对，不用零填充或命中率掩盖。

将当前发布版 `AuctionCalculator` 公式应用到 09:25:06 前每只股票最后一条
真实 TD tick 后，`am/br/ar` 均为 5171/5171 与 t1-v2 Redis Q2 一致。这证明
t1-v2 当前公式对 TD 输入自洽；不证明 `auction_snapshot_v2` amount/rest 列
与 Q2 同口径。

## 主线对齐

```text
Rabbit-primary shape       保持
TD one 3s read supply      保持
同一 t1-v2 Q2 producer     真实发布包
回放 Redis                  仅隔离 DB/前缀
TD write                   关闭
09:25:06 empty Clock       已证明
Rabbit arrival parity      UNKNOWN
```

因此 TASK-008 仍为 `PARTIAL_EVIDENCE`，不能升级为 NORMAL；M3-1 继续：

```text
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

完整证据目录：

```text
/home/exedev/validation/td-rabbit-phase-f-0924-0925-20260924T194854+0800/
```

下一步应先核对 `match_amt/rest_*` 的同版源字段语义和快照生产版本，再决定
是否进入下一阶段；不得把这次价格/涨跌幅命中误报为完整快照 parity。
