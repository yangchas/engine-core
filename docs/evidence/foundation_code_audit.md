# Foundation wheels code audit

Audit date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/fix-foundation-wheels`

## Verification performed

- `python -m pytest -q`: 50 passed on local Python 3.9.13 compatibility smoke.
- `python -m compileall -q src tests examples`: passed.
- `git diff --check`: passed.
- Formal server runtime interpreter on `cobra-ion` is Python 3.12.3. The current
  `src`/`tests` were copied to the explicit temporary validation directory
  `/home/exedev/tmp/engine_core_validation_20260904` and passed with
  `PYTHONPATH=src`: 50 tests passed. No production process or data was changed.
- No source file imports the legacy `engine_next` project, Redis client, TD
  client, RabbitMQ client, or network library.

## Findings

### Q2 amount/volume semantics are verified at the legacy producer boundary

`C/t1_v2` converts source amount to rounded integer yuan, carries volume as
shares, and computes rolling amount deltas from cumulative amount. Its auction
calculator writes `am`, `br` and `ar` as integer-yuan values; `br/ar` are
level-2 price/quantity proxies. The Python fact layer still requires callers
to pass explicit `CUMULATIVE` semantics, so no unverified field is inferred.

### P1: TD daily-kline volume semantics are not yet verified

The live probe read `daily_kline` successfully but sampled rows had
`volume=0`. This is recorded as evidence only; it is not treated as a semantic
zero or a missing value. The first real TD provider must keep this field
explicitly unresolved until the legacy writer/consumer path is traced.

### P1: Redis Q2 active cohort is not an immutable historical snapshot

The corrected Adapter reads the legacy compact key and gets full coverage for
`q2:active:20260904`. Reading `q2:active:20260903` later found 5,216 of 5,217
hashes with a source timestamp on 2026-09-04, so the strict date check reports
`PARTIAL`. This is why the Adapter does not silently accept a stale active set
as a historical replay frame. The source timestamp meaning and cross-day update
order remain UNKNOWN.

### P2: The real 09:20 -> 09:24 Q2 case is not available yet

The server currently exposes a Redis cohort for 2026-09-03 and archived Q2
ground-truth files around 09:30 on 2026-08-26, but no paired real Q2 snapshots
at the legacy 09:20:03 and 09:24:10 firing anchors were found. The current
09:20 -> 09:24 fact test is therefore a deterministic fixture case, while the
captured production fixture is used for Q2 normalization only.

### P2: Checkpoint, Engine ordering and replay adapters remain intentionally deferred

No code in this branch claims Rabbit arrival replay, authoritative journal
semantics, generic watermarking, outbox/fencing or full recovery equivalence.
Those are deferred by design and must not be inferred from the current tests.

## Audit conclusion

No P0 correctness issue was found in the implemented foundation wheels after
the fixes recorded in this branch. The remaining P1/P2 items are explicit data
semantic and integration gates, not hidden defaults. Proceed next with a
server-side, read-only TD/Redis contract test and only then capture the paired
real segment fixtures.
