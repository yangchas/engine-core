# t1-v2 runtime progress evidence (2026-09-11)

## Scope

This is a read-only extraction from Cobra-ion's existing runtime log. It is
evidence about periodic runtime counters around the auction period, not a new
consumer or an instrumentation change.

- service: `t1-v2-live.service`
- log: `/home/exedev/t1v2work/logs/t1_v2.log`
- log SHA-256: `a921fd4e678f53c3675a40948d0aa5ad286c7b6cc9a4bd1308ae2695bc151bf1`
- observation window: `2026-09-11 09:19:01` through `09:25:01` (CST)

## Observed counters

The existing progress records show continuously increasing `batches`,
`source_in`, `ack`, `ticks`, `redis_committed`, and `td_sql`. `last_ts_ms`
advances in one-second source-time steps. Around 09:24–09:25 the records were:

```text
09:24:00 batches=54673 source_in=41078421 ack=54673 ticks=39244219 last_ts_ms=1789089840000
09:24:11 batches=54696 source_in=41088938 ack=54696 ticks=39254736 last_ts_ms=1789089850000
09:24:20 batches=54720 source_in=41099682 ack=54720 ticks=39265478 last_ts_ms=1789089860000
09:24:30 batches=54745 source_in=41112567 ack=54745 ticks=39278361 last_ts_ms=1789089870000
09:24:40 batches=54771 source_in=41129089 ack=54771 ticks=39294881 last_ts_ms=1789089880000
09:24:51 batches=54797 source_in=41145833 ack=54797 ticks=39311625 last_ts_ms=1789089890000
09:25:01 batches=54823 source_in=41163691 ack=54823 ticks=39329483 last_ts_ms=1789089900000
```

The same records show `ack_fail=0` for these samples. `source_reject`,
`last_reject`, and `last_ticks` remain separately visible, so rejected input is
not silently treated as accepted tick volume.

## What this proves

- The running t1-v2 process was receiving and acknowledging input around the
  observed period.
- The runtime progress log preserves a source-time indicator and exposes
  separate receive/reject/ack/tick counters.
- The existing log can support a coarse service-health timeline without
  touching RabbitMQ, Redis writers, TD writers, or acknowledgements.

## What this does not prove

The log has no per-batch identifier, source timestamp range, `emit_a25` flag,
AuctionState update marker, or Redis/TD writer ordering. Therefore it cannot
prove which individual ticks were in the same batch or whether a particular
09:25 tick preceded the finalization trigger. `FINAL_TICK_BATCH_MEMBERSHIP`
and source-to-writer causal ordering remain `UNKNOWN`.

This evidence must not be used to infer a producer fix, a 09:25 barrier, or
an exact Redis/TD projection equivalence.
