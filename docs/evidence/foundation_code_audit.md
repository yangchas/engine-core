# Foundation wheels code audit

Audit date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/fix-foundation-wheels`

## Verification performed

- `python -m pytest -q`: 45 passed on local Python 3.9.13 compatibility smoke.
- `python -m compileall -q src tests examples`: passed.
- `git diff --check`: passed.
- Formal server runtime interpreter exists on `cobra-ion`: Python 3.12.3. The new
  project has not been deployed or executed remotely yet; remote verification so
  far is limited to read-only Redis/TD probes through the existing legacy venv.
- No source file imports the legacy `engine_next` project, Redis client, TD
  client, RabbitMQ client, or network library.

## Findings

### P1: Q2 amount/volume semantics are not yet verified

`normalize_q2` preserves integer source-native values and the fact layer
requires explicit `CUMULATIVE` semantics before computing deltas. The producer
writes integer yuan/unit fields, but the exact meaning of `amt`, `vol`, `am`,
`br`, `ar` remains UNKNOWN. Do not change the fact call sites to infer units or
fallback to zero until a producer/consumer differential fixture verifies them.

### P1: TD daily-kline volume semantics are not yet verified

The live probe read `daily_kline` successfully but sampled rows had
`volume=0`. This is recorded as evidence only; it is not treated as a semantic
zero or a missing value. The first real TD provider must keep this field
explicitly unresolved until the legacy writer/consumer path is traced.

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
