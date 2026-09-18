# M3-1 exact-commit recovery verification — 2026-09-18

## Scope

The Git archive of Core commit `6b4f726` was extracted to the new isolated
Cobra-ion directory:

```text
/home/exedev/validation/engine-core-6b4f726-v1
```

This directory is separate from the production release and from earlier
validation copies.

## Formal Linux verification

```text
Python: 3.12.3
pytest: 525 passed
compileall: PASS
```

The archive's `src/engine_core/data.py` SHA-256 is:

```text
17b1d537a2ae1827dc39809be0b50a1c13202310c9e8cbb555ffa5cb46d32f45
```

## Real Redis read-only recovery run

Runner:

```text
examples/run_m3_0920_shadow.py
trade_date=2026-09-18
symbol=000338
origin=RECOVERY_CATCHUP
```

Artifact:

```text
/home/exedev/validation/m3-0920-shadow-20260918-recovery-6b4f726-000338.json
sha256=f918bd7daab956831ddae70bee94b06dc4309cd8dd7d6c3be93d0cc07245a9bf
```

Observed result:

```text
q2 coverage=1.0
q2 status=STALE
q2 consistency=BEST_EFFORT_STALE
prefetch_calls=1
origin=RECOVERY_CATCHUP
preflight_gate=BLOCKED
node_dispatched=false
engine=null
```

The block reason included `q2_observed_after_0920_firing`. This is the expected
fail-closed result for a post-market start and is not a normal-origin 09:20
acceptance. `engine-next` and `t1-v2-live` remained active; no Rabbit, Redis,
TD, notification, or effect path was modified.
