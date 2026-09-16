# Real Redis Previous-Day Stats Evidence — 2026-09-16

## Scope

- Core commit: `b0f8f4b`
- Branch: `codex/feature-session-engine-integration`
- Formal runtime: `cobra-ion`
- Python: production `engine-next` Python 3.12 environment
- Safety boundary: bounded Redis reads and bounded TD `SELECT`; no Redis/TD write, Rabbit consume/ACK, repair, network fallback, notification, or order effect

## Exact verification

- Git archive: `/home/exedev/validation/engine-core-b0f8f4b.tar`
- Archive SHA-256: `5753ad60eb632beeac3268600d6b1404f3206ea66aae3f51082fba2721499e6f`
- Local suite: `436 passed`
- Cobra-ion suite: `436 passed`
- Local and cobra-ion `compileall`: PASS
- Production services after the probe: `engine-next=active`, `t1-v2-live=active`

## Real source result

The bounded TD daily-kline query for `000001`, `000002`, and `600519` on
`2026-09-15` returned no rows.  The audit runner then explicitly selected the
existing Redis runtime view `cache:kline_ready:2026-09-15`; this is a visible
audit-source choice, not a hidden fallback engine.

- Source selection: `redis_kline_ready_after_td_empty`
- Core source: `redis_daily_kline_cache`
- Requested symbols found: `3/3`
- Canonical result status: `UNAVAILABLE`
- Reason: `available_at_unknown`
- Result content hash: `c77d46a840b1708dc57068553f98f9aea6070faeaaa0ab4d5126b6fc1f89cc37`

The Redis rows prove that real previous-day close/amount facts are present.
They do not prove when those rows first became historically knowable.  A
read-only scan found no `*kline*meta*` or `*watermark*` Redis keys, so Core did
not infer `available_at` from query time, Redis presence, or current wall time.

## Full readiness observation

- Q2: `5221/5221`, row coverage `1.0`, status `PARTIAL`, all rows stale at the late-afternoon observation cutoff
- Previous-day stats: `UNAVAILABLE` (real Redis rows, unknown availability)
- Previous-day limit pool: `UNAVAILABLE` (real 32-row cache, production metadata predates the formal schema/units contract)
- Hot plates: `UNAVAILABLE` (real 50-row cache, production metadata predates the formal schema/units/hash contract)
- Overall startup readiness: `PARTIAL`

Real run artifact:

- `/home/exedev/validation/real-auction-reference-readiness-20260916-b0f8f4b.json`
- SHA-256: `c1c842a19d69766261f902c49b2da9650b7f39044f46ee90e7488e2e6678a935`

## Contract conclusion

This change closes a source-coverage gap without weakening temporal safety:

```text
TD rows present
    -> use TD result

TD read succeeds but is empty
    -> explicitly inspect existing Redis date-bucketed view

Redis rows present but historical availability unproven
    -> data presence is preserved
    -> runtime/replay status remains UNAVAILABLE

TD access error
    -> preserve TD ERROR
    -> do not hide it behind Redis
```

The remaining blocker is source-side availability metadata for
`cache:kline_ready:{date}`.  It must be emitted when a persisted response is
successfully verified; Core will not invent it.
