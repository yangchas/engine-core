# Foundation wheels code audit

Audit date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/fix-foundation-wheels`

## Verification performed

- `python -m pytest -q`: 55 passed locally, including the real 600519 auction
  fixture and semantic-hash regression tests.
- `python -m compileall -q src tests examples`: passed.
- `git diff --check`: passed.
- Formal server runtime interpreter on `cobra-ion` is Python 3.12.3. The current
  `src`/`tests` were copied to the explicit temporary validation directory
  `/home/exedev/tmp/engine_core_validation_20260904_1244` and passed with
  `PYTHONPATH=src`: 55 tests passed, plus `compileall`. No production process
  or data was changed.
- No source file imports the legacy `engine_next` project, Redis client, TD
  client, RabbitMQ client, or network library.

## Findings

### Q2 amount/volume/time semantics

`C/t1_v2` converts source amount to rounded integer yuan, carries volume as
board lots, and computes rolling amount deltas from cumulative amount. Its
auction calculator writes `am`, `br` and `ar` as integer-yuan values; `br/ar`
are level-2 price/quantity proxies. On cobra-ion, Q2 `ts` aligns with the
corresponding `stock_tick_v2.ts` for the sampled symbols. The canonical name is
`source_record_time_ms`; its upstream vendor meaning and Rabbit arrival meaning
remain UNKNOWN. The Python fact layer requires callers to pass explicit
semantics, so no unverified field is inferred. Auction matched amount uses an
`OBSERVED_STATE` point difference rather than being mislabeled as a cumulative
counter.

### Previous-day row boundary is thin and fail-closed

`normalize_previous_day_stats_rows` adapts verified legacy daily-kline rows
without reimplementing the connection/query path. It requires a six-digit
symbol plus finite `close` and `amount`, preserves explicit zero values,
rejects duplicates and qualified symbols, and emits stable symbol ordering.
An empty normalized row set is treated as `MISSING` by the provider wrapper.

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

### Real 09:20 -> 09:24 auction evidence

The server has real `auction_snapshot_v2` rows at the legacy 09:20:03 and
09:24:10 firing anchors. The 600519 pair, plus the first observed 09:15:08
`stock_tick_v2` row, is captured in
`docs/evidence/real_data_probe/20260904T124403+0800/auction_segment_600519_20260903.json`.
The business intervals remain `[09:15:00,09:20:00)` and `[09:20:00,09:24:00)`;
source record times are separate and Segment A is explicitly PARTIAL.

### P2: Checkpoint, Engine ordering and replay adapters remain intentionally deferred

No code in this branch claims Rabbit arrival replay, authoritative journal
semantics, generic watermarking, outbox/fencing or full recovery equivalence.
Those are deferred by design and must not be inferred from the current tests.

## Audit conclusion

No P0 correctness issue was found in the implemented foundation wheels after
the fixes recorded in this branch. Remaining UNKNOWNs are limited to upstream
timestamp definition and `daily_kline.volume` semantics; neither is used to
make a stronger inference. Proceed with the real fixture differential test,
then stop Foundation without adding Engine/Replay/Checkpoint mechanisms.
