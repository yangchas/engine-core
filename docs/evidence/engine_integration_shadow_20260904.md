# Live Q2 + TD shadow integration

Date: 2026-09-04 (Asia/Shanghai)
Host: `cobra-ion`
Remote code copy: `/home/exedev/tmp/engine_core_validation_20260904_1244`

## Path exercised

```text
Redis Q2
  -> Q2ProjectionSnapshot
  -> DeterministicEngine
  -> EngineSnapshot
  -> TDPreviousDayStatsProvider (existing read-only taos path)
  -> PreviousDayStatsFunction
  -> TemporalDataGuard
  -> FrozenDataBundle
  -> SegmentFrame / Fact
  -> DATA_READY
  -> ProbeStrategy trace
```

## Observed result

```text
active_symbols=5217
projection_quotes=5217
q2_status=PARTIAL
q2_coverage=1.0
q2_stale_symbols=10
td_status=UNAVAILABLE
td_actual_trade_date=2026-09-03
td_available_at_ms=None
bundle_completeness=0.0
segment_quality=PARTIAL
price_status=READY
pressure_status=READY
lineage_refs=1
strategy_evaluations=2
trace_completeness=PARTIAL
```

`UNAVAILABLE` is expected: the real TD query returned the correct previous
trade date, but no historical availability timestamp was available. The
TemporalDataGuard correctly refused to promote an observed query result into
runtime-ready historical input.

No Redis/TD write, RabbitMQ delivery, production process, or external effect
was performed.
