# Core real Redis theme-to-Engine shadow — 2026-09-28

## Purpose

Close one feature-specific Core migration seam: take the real Redis auction
projection/theme compatibility facts already used by the Core shadow rule,
bind them through `DATA_READY` to `AuctionShadowStrategy`, and verify the
Engine-produced trace against the direct compatibility trace.

This is not queue-backlog diagnosis, replay-order proof, normal-opening
acceptance, or a strategy replacement. It is a post-market diagnostic using
the frozen Redis values visible to the process during this run.

## Implementation

- Added `build_real_theme_delta_strategy_shadow_from_projections()` so the
  existing direct runner and Engine integration can consume one already-read
  0924/0925 cohort.
- Added `examples/run_real_theme_delta_engine_shadow.py`. Its Redis facade
  permits and records `HGETALL` only. It loads the 0920/0924/0925 Redis
  projections and the existing theme mapping hashes, checks that the theme
  result's 0924/0925 content hashes are the same cohort submitted to Core, and
  drives one in-memory `DeterministicEngine` session.
- The 0925 theme `DataResult` uses `PARTIAL`, `available_at_ms=None`, and the
  existing `TemporalDataGuard` live-fetch marker. This proves only that this
  process completed the read before the diagnostic evaluation; it does not
  claim historical availability.
- Added regression tests for one-time 0925 `DATA_READY` binding, direct-vs-
  Engine trace equality, and rejection of mismatched frozen cohorts. These are
  wiring/unit tests, not real-market acceptance evidence.

## Real Redis run

Artifact:

```text
/home/exedev/validation/core-theme-engine-shadow-20260928T152156+0800/real_theme_delta_engine_shadow.json
SHA-256: 878d7f449023ce3e21500e5eec97e5b48fb8715a1dee34633eaa37f891387389
```

Observed result:

```text
trade_date=2026-09-28
run_mode=POSTMARKET_DIAGNOSTIC
Redis reads=5 HGETALL commands; keys limited to 0920/0924/0925 and two theme maps
0920/0924/0925=READY, 200 TOP_AMOUNT rows each
source times are recorded per projection; process observation times and
post-market diagnostic logical times are separate
theme rows=178, mapped=175, mapping missing=3
theme DataResult=PARTIAL
single Engine session=true
processed_signals=7, strategy_result_count=3, pending_evaluations=0
same_frozen_theme_cohort=true
engine_matches_direct_theme_shadow=true
decision_status=FACT_ONLY
```

The single-symbol auction fact is also `PARTIAL`; its symbol was selected from
the intersection of the three `TOP_AMOUNT` projections. The theme label counts
are bounded to the current Redis TopN intersection. They are not full-market
coverage and are not an independent comparison with the deployed legacy
report output.

Side-effect audit from the runner:

```text
Redis writes=0
TD commands=0
Rabbit consumes=0
effects=0
```

## Verification

```text
targeted tests: 21 passed
full suite: 707 passed, 3 upstream deprecation warnings
compileall: PASS
git diff --check: PASS
```

## Alignment and limits

This advances Core from “real Redis direct shadow + fixture-only Engine
composition” to “real frozen Redis auction/theme cohort through Core Engine”.
It does not establish that the snapshot was available at 09:20/09:24/09:25,
Rabbit arrival ordering, full-market membership, NORMAL acceptance, or
production replacement readiness. Historical `available_at` and Rabbit
arrival order remain `UNKNOWN`; `M3_1_NORMAL=BLOCKED` and
`TD_WRITE_HEALTH=UNPROVEN` are unchanged.

Do not promote a new phase based solely on this slice. The next migration
question should remain feature-scoped and use real frozen source evidence; it
must not restart queue-backlog investigation or infer production acceptance
from this diagnostic.
