# Production observability snapshot — 2026-09-13

## Identity and safety

- Host: `cobra-ion`
- Read-only command: `systemctl show` and `journalctl -u ... -n ...`
- No service restart, producer change, Redis/TD write, Rabbit consumer or ACK action
- `engine-next` release: `e272842c8f490f55a1b017badb71e71904ce008e`
- `t1-v2-live` process: PID `2878024`, active since `2026-09-09 22:31:00 CST`
- `engine-next` process: PID `3181295`, active since `2026-09-11 00:30:02 CST`

## Latest t1-v2 progress record

The latest retained progress line was timestamped `2026-09-12 07:00:20 CST`:

```text
batches=109450
source_in=84281451
source_reject=3666944
ack=109450
ack_fail=0
reject=5243
ticks=80614507
redis_cmds=217764708
td_sql=109465
redis_committed=108741822
last_in=1000
last_reject=0
last_ticks=1000
pipeline_ms=17
commit_ms=172
ack_ms=0
last_ts_ms=1789111770000
wall_lag_ms=55850095
```

`last_ts_ms` converts to `2026-09-11T15:29:30+00:00`; the large wall lag is
consistent with the record being collected on a non-trading Saturday, not a
fresh 09:25 trading observation. The counters prove that the deployed t1-v2
binary is emitting the expected observability fields and that the latest
retained line had zero ACK failures. They do not prove that all source rejects
are harmless, nor do they identify the first production-chain divergence.

`engine-next.service` had no journal entries in the inspected tail, so its
report/consumer lifecycle remains unproven by this snapshot.

## Gate classification

```text
T1_PROGRESS_COUNTERS_PRESENT       PASS
LATEST_ACK_FAILURES                PASS (0 in retained line)
TRADING_DAY_0925_BATCH_EVIDENCE    UNKNOWN (weekend snapshot)
SOURCE_REJECT_SEMANTICS            UNKNOWN
ENGINE_NEXT_REPORT_LIFECYCLE        UNKNOWN
PRODUCTION_CHAIN_ACCEPTANCE         NOT EVALUATED
```

## Next trading-day delta capture

At 09:10, 09:20, 09:24, 09:25 and 09:30 record the difference from the prior
progress line, not only cumulative totals. Preserve `batches`, `source_in`,
`source_reject`, `ack`, `ack_fail`, `ticks`, `redis_committed`, `td_sql`,
`pipeline_ms`, `commit_ms`, `ack_ms`, `last_ts_ms` and `wall_lag_ms`. The
capture remains journal-only/read-only; it must not add a Rabbit consumer.

