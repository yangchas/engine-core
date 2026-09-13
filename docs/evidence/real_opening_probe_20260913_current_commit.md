# Real Redis opening probe — 2026-09-13

## Identity

- Core archive: `ed3547c5799e6b72dcdcc79f1f13d500616ac0fa`
- Host: `cobra-ion`
- Python: `/home/exedev/services/engine-next/shared/venv/bin/python` (3.12.3)
- Requested trade date: `2026-09-10`
- Symbols: `600519`, `000001`, `000002`
- Freshness budget: `300000 ms`
- Mode: Redis `SMEMBERS`/`HGETALL` only; no writes, TD, Rabbit, repair or effects

## Observed result

The real Redis `q2:active:20260910` cohort was empty on the Sunday probe:

```text
projection_status      MISSING
projection_consistency EMPTY_UNIVERSE
coverage               0.0
expected_symbol_count  0
quote_count            0
freshness_status       MISSING
```

The selected symbols were therefore returned as `unavailable` with reason
`symbol_not_in_q2_cohort`. The runner did not convert an empty cohort into a
fresh state. Projection and result hashes were deterministic for this exact
observation; the output artifact SHA-256 was
`a2ed35f316d13cc9d32922304d1f4737d7d095eaa20f9130c2458a75971d0a62`.

## Gate classification

```text
REAL_REDIS_CONNECTIVITY       PASS
EMPTY_COHORT_CLASSIFICATION   PASS
FRESHNESS_REPORTING           PASS
LIVE_Q2_COVERAGE              NOT_AVAILABLE
PRODUCTION_REPLACEMENT        NOT_CLAIMED
```

The result is a real read-only production-data observation, not a unit test.
Because the cohort was empty, it is evidence of truthful missing-data handling,
not evidence that the historical date has usable live Q2 coverage.

