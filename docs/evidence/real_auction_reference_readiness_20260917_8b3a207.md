# Real auction-reference readiness — 2026-09-17 premarket

## Exact build

- Core commit: `8b3a207`
- Archive SHA-256: `07baca58744d4a189ad2aff7b3744a7bcaff5c3740e89812080d4771c64c0043`
- Cobra-ion: Python 3.12.3, `438 passed`, compileall PASS
- Artifact: `/home/exedev/validation/real-auction-reference-readiness-20260917-8b3a207.json`
- Artifact SHA-256: `02da2b71e7e2f2401480ed28ec746145cb555f585746e1194b16a20219a33788`

## Runtime safety

- `engine-next`: active, `NRestarts=0` at observation; system start timestamp `2026-09-17 00:30:01 CST`
- `t1-v2-live`: active, `NRestarts=0`
- No new Rabbit consumer or ACK change
- No Redis/TD write, repair, network fallback, notification, or effect
- Shadow schedule is self-owned and isolated from both production systemd units

## Real observations

- Trade date: `2026-09-17`
- Derived previous trade date: `2026-09-16`
- Q2: `MISSING`, expected universe was not yet published at `00:39 CST`
- Previous-day limit pool: 89 real Redis rows; old production metadata has no formal schema/units/payload contract, so Core returns `UNAVAILABLE`
- Previous-day daily stats: TD bounded query empty; existing Redis `cache:kline_ready:2026-09-16` was selected explicitly and contained real rows for the bounded symbols, but no `cache:kline_ready_meta:2026-09-16` was present, so Core returns `UNAVAILABLE` with `available_at_unknown`
- Hot plates: no current-date Redis key yet, so `MISSING`
- Overall readiness: `BLOCKED` / `WAIT_FOR_Q2`

## Contract conclusion

The probe confirms that Core can see the real date-bucketed Redis kline view without changing the existing source chain. It does not grant historical availability merely because the rows exist. The source-side candidate commit `68134f9` adds a date-level metadata proof after offline sync writes are read back; it has not been deployed to production.

The premarket `MISSING`/`UNAVAILABLE` results are truthful and expected. They must not be converted to READY by using current query time, Redis key presence, or an inferred source publication time.
