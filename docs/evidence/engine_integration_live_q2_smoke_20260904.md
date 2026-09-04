# Engine integration live-Q2 smoke

Date: 2026-09-04 (Asia/Shanghai)
Host: `cobra-ion`
Remote copy: `/home/exedev/tmp/engine_core_validation_20260904_1244`

## Scope

Read-only smoke test against the existing local Redis Q2 endpoint. The test
read the first 64 symbols from `q2:active:20260904`, normalized the existing
hashes with `build_q2_projection`, and submitted one `MARKET_UPDATE` followed
by one `TIMER` to the in-memory `DeterministicEngine`. No production write,
RabbitMQ delivery, or external effect was performed.

## Result

```text
symbols=64
projection_status=PARTIAL
coverage=1.0
quotes=64
snapshots=1
strategy_results=1
window_finality=FINAL
```

The `PARTIAL` result was expected under the explicit `stale_after_ms=120000`
probe policy: one quote was stale and no symbol was missing. This is an
observation of the current projection freshness, not a production freshness
default.

## Boundary confirmed

```text
existing Redis read path
    -> Q2 normalization/projection
    -> CurrentMarketState reducer
    -> WindowManager
    -> EngineSnapshot
    -> ProbeStrategy
```

This confirms that the current Engine slice can consume a real Q2 projection
without importing the legacy runtime or mutating the source systems. It does
not prove historical replay, Rabbit arrival equivalence, or production
availability semantics.
