# TASK-008 — direct Redis → Core readback and 09:25 engine shadow

Observed: 2026-10-01 (+08:00)
Trade date: `2026-09-29`
Result: `PASS_WITH_LIMITS` for the isolated replay-to-Core path; TASK-008 remains `PARTIAL_EVIDENCE`.

## What was exercised

The deployed t1-v2 release binary (`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`)
read real `market_data1.stock_tick_v2` rows for `[09:15:00, 09:25:12)` and wrote
to a unique Redis DB15 validation namespace. It processed 603 batches and
193,994 source ticks; `source_reject=0`, `ack=0`, `td_sql=0`, and
`quote_state_committed=193,983`. The 11-count difference between source ticks
and Q2 commits is not classified here; it is not labeled as dropped data.

Core then read that actual DB15 namespace through
`RedisQ2ProjectionAdapter` (`SMEMBERS` + per-symbol `HGETALL`) and passed the
result through `MarketStateReducer` and the public `DeterministicEngine` queue:

- Q2 active cohort: 5,223 symbols; 5,223 hashes read; zero missing hashes.
- Coverage `1.0` is coverage of this producer active set only; full-market
  universe coverage remains `UNKNOWN`.
- A 09:25 `MARKET_UPDATE` and `TIMER` generated 5,223 standalone 09:25 anchor
  facts: 5,070 `AVAILABLE`, 153 `MISSING`.
- With no fabricated 09:20/09:24 inputs, cross-anchor results remained
  `PENDING`; the standalone 09:25 facts were still produced. Decision remained
  `FACT_ONLY`.
- Core's Q2 auction aggregate matched the t1-v2 Redis summary field-by-field:
  5,205 candidates, 5,070 valid, 135 unavailable, amount `11,368,130,630`,
  limit-up 7, limit-down 6, and limit-up bid amount `32,232,283`. Its semantic
  content hash also matched the prior Core run over the same Q2Frame artifact.

The Core Redis read completed at `2026-10-01 14:40:54 +08:00`; this is the
actual observation time of the validation read, not historical 09:25 Redis
availability. No freshness cutoff was applied because this was an archived
replay cohort. `READY` therefore describes projection parsing, not freshness
or historical visibility. `historical_available_at=UNKNOWN`.

The separate Engine shadow used the 09:25:06 logical evaluation/freeze time
with that post-hoc Redis observation. Its `late_execution` field describes
logical evaluation against the auction timing policy; `false` at the virtual
09:25:06 evaluation does not claim the Oct-01 read happened on time.

## Frozen-anchor reconciliation

The comparison used the exact retained DB0 key
`market:auction:anchor:20260929`, after its raw SHA-256 matched the preserved
audit value `50d4785eb96631a1400a73422b95b5961c3b8ed1d1738d9111a8eaa8f1f8231a`.
Across 5,200 shared symbols, 467 `amount` values and 473 `bid_amount` values
differ from the replay's final state; 480 symbols differ as an amount/bid pair.
All 480 frozen pairs also occur together in an earlier state of the same
event-time replay. Their last matching replay-frame lag relative to replay
final state was 1–3 seconds for 451 symbols, 3–10 seconds for 18, and over 10
seconds for 11.

This supports a per-symbol state-cut/cohort explanation and weakens a simple
formula-mismatch explanation for those rows. The lag is an event-time replay
frame difference, **not** Rabbit arrival latency or proof that the earlier
state was visible in Redis at the historical freeze. Arrival order,
`available_at`, and the exact original per-symbol visibility cut remain
`UNKNOWN`. No exact-second gate or formula change follows from this result.

Correction to the investigation: an initial exploratory comparison used
`task008-live-freeze-20260930-0925/live_anchor_observation_2.json`, which is a
2026-09-30 capture (its paired TD rows carry that date), not the retained
2026-09-29 freeze. That comparison was discarded. The reported reconciliation
above uses the exact 2026-09-29 DB0 key and verifies its retained SHA before
comparing values.

## Evidence and side-effect boundary

- Direct Core Redis readback:
  `/home/exedev/validation/task008-0929-isolated-redis-replay-20261001T141712+0800/core_redis_readback_audit.json`
  SHA-256 `cdb50515ac207ed6b813a69a680ac6a8545710672354df369dfa53dcdeb72f0f`
- Core Engine 09:25 shadow:
  `/home/exedev/validation/task008-0929-isolated-redis-replay-20261001T141712+0800/core_redis_engine_0925_shadow.json`
  SHA-256 `d222686feba82a1621ad05154d0b74a1ee8218b4169792bfda72c8185a894915`
- Frozen/replay state-history comparison:
  `/home/exedev/validation/task008-0929-isolated-redis-replay-20261001T141712+0800/freeze_vs_replay_state_history.json`
  SHA-256 `5b2b7af2ea017753c292da7efa7f7f5a15f0a1fe7ac1d298428cfb31ed364d1c`
- The t1-v2 replay intentionally wrote only to its unique DB15 namespace.
  Core's DB15 reads and the DB0 frozen-key `GET` were read-only. No TD writes,
  Rabbit access, production Redis writes, service changes, or restarts occurred.

Core verification after the existing observation-time propagation change:
`787 passed`; compileall and `git diff --check` passed. No production code was
changed or deployed by this audit.

## Status and next use

```text
TD_TO_T1_V2_ISOLATED_REDIS=PASS_WITH_LIMITS
CORE_DIRECT_REDIS_READBACK=PASS_WITH_LIMITS
CORE_ENGINE_0925_STANDALONE_FACTS=PASS_WITH_LIMITS
CORE_Q2_SUMMARY_VS_T1_REPLAY=PASS
REPLAY_VS_RETAINED_LIVE_FREEZE_AMOUNT_BID=MISMATCH_OBSERVED
STATE_CUT_HYPOTHESIS=SUPPORTED_BUT_UNPROVEN
RABBIT_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
FULL_MARKET_COVERAGE=UNKNOWN
TASK-008=PARTIAL_EVIDENCE
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

The next development may use this pinned event-time Q2 cohort through Core,
with field-level provenance and the above limitations. Continue to treat
historical freeze parity for amount/rest-bid as unresolved; do not force the
replay to match by deleting rows, shifting a hard timing gate, or changing the
producer formula without new evidence.
