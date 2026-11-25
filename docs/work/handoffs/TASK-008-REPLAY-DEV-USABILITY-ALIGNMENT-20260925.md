# TASK-008 replay development usability alignment — 2026-09-25

## Decision

The project needs a replay that follows the real calculation path closely
enough to develop and evolve the live system. It does not require a perfect
historical Redis image before replay can be useful.

```text
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
TASK_008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
LIVE_RABBIT_EQUIVALENCE=UNKNOWN
```

Historical `available_at`, Rabbit delivery membership/order, and exact
wall-clock visibility constrain claims about what was live at a particular
instant. They do not block replay-led development of behavior whose required
inputs, field semantics, and source version are covered by evidence. Assess
each feature against the fields it actually consumes; an unverified field is
not a parity or correctness oracle. Missing, stale, partial, and unavailable
facts remain visible in each result.

## Existing real-source basis

- Phase N replayed real TD for 2026-09-24 `[09:15:00,09:32:09)` through the
  exact t1-v2 release into isolated Redis DB7 and DB8. It processed 429,392
  rows, produced 5,222 Q2 hashes, and repeated with zero normalized semantic
  key differences. Core read the 09:32:09 projection and produced matching
  ordered/shuffled hashes; 24 quotes were stale under the 10-second policy.
  Evidence: `TD_RABBIT_PHASE_N_AUDIT_20260924.md`.
- Phase P full-window replay processed real 2026-09-23 TD in 500 three-second
  SELECT slices through t1-v2 into isolated Redis. Core readback covered 5,222
  Q2 symbols and repeated Q2/auction semantic outputs matched. At the end
  cutoff, 68 Q2 quotes were stale and the result remained `PARTIAL`.
  Evidence: `TD_RABBIT_PHASE_P_FULL_REPLAY_ISOLATED_REDIS_AUDIT_20260925.md`.

These runs are useful development baselines because they use real market rows,
the t1-v2 Q2 producer path, and Core readback. Their result quality and source
version must remain attached to any feature comparison.

Known field limit: the 2026-09-18 Phase F snapshot comparison matched
comparable price (`4408/4408`) and change (`5209/5209`), but match amount had
1,576/5,209 mismatches, rest bid had 1,626/5,209, and rest ask had 1,621/5,209.
Do not use those amount/rest fields as parity oracles until the source version
and field semantics are reconciled. This limits features depending on those
fields, not unrelated replay development. See
`TD_RABBIT_PHASE_F_AUDIT_20260924.md`.

## Current Redis probe interpretation

The 2026-09-25 direct Redis probe read the date-scoped active set and global
`q2:<symbol>` hashes after the 2026-09-24 session. It is not a point-in-time
09:32 capture. Running its rows at the 09:32:10 event-time cutoff correctly
excluded later timestamps and returned `NOT_COMPARABLE` for the four selected
Engine samples. A 15:00:03 control run showed deterministic processing only;
it did not establish historical availability or opening correctness.

Evidence: `/home/exedev/validation/task008-real-redis-audit-20260925-qlzb4egc/`.

## Mainline alignment

Continue replay-led development using immutable real TD/t1-v2/Q2 artifacts and
the matching t1-v2 calculation source. Compare candidate outputs against the
baseline at Q2, 0920/0924/0925 anchors, and Core facts; keep stale/missing
coverage explicit and investigate differences that affect the feature under
development. Use controlled live shadow when a feature depends on Rabbit
arrival, wall-clock visibility, or the production freeze instant. Do not make
those unknowns a general development gate, and do not label replay results as
NORMAL acceptance or deployment parity.

No code semantics or production gates changed in this alignment. TASK-008
remains the current task; this note does not create or auto-promote another
task.
