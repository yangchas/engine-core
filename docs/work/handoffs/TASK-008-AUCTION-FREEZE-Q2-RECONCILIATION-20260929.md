# TASK-008 — 2026-09-29 frozen 09:25 anchor vs later Q2

Audit time: 2026-09-29 14:24 +08:00\
Core branch: `codex/feature-session-engine-integration`\
Core HEAD: `14a3a44`\
Result: `PARTIAL_EVIDENCE`

## Purpose

Compare the actual 09:25 frozen auction output with a later current Q2 read,
using Core's existing TD snapshot adapter, Redis Q2 adapter, and auction fact
builder. This is a same-day source reconciliation, not a Rabbit replay or
historical Redis-availability reconstruction.

## Source reads

- Redis DB0: `PING`, `GET market:auction:anchor:20260929`,
  `HGETALL market:auction:20260929:0925`, and Core's
  `SMEMBERS q2:active:20260929` plus per-symbol `HGETALL q2:<symbol>`.
- TD: one read-only `SELECT` from
  `market_data1.auction_snapshot_v2` for `trade_date="20260929"` and tags
  `0920`, `0924`, `0925`.
- No Rabbit access, Redis writes, TD writes, service changes, or source-code
  changes occurred.

## Observed facts

- Redis's frozen `market:auction:anchor:20260929` contains 5,200 symbols.
  The 09:25 hash metadata reports `tag=0925`, `n=5200`, and
  `ts=1790645106154` (`09:25:06.154 +08:00`); `top_amount` is a 200-row
  projection and is not full-market evidence.
- TD returned 5,223 rows for each anchor. Core's
  `build_snapshots_from_rows` mapped all three real TD rows for all 5,223
  symbols; no symbol was skipped. The 09:25 TD source timestamp is
  `09:25:06.154 +08:00`.
- The 5,200 symbols shared by the frozen Redis anchor and TD agree exactly on
  `amount`/`match_amt_yuan` and `bid_amount`/`rest_bid_amt_yuan` (5,200/5,200
  for each field). The remaining 23 TD symbols have unavailable 09:25 price.
  Redis and TD are fan-outs of the same t1-v2 state, so this proves their
  stored projections agree; it is not independent proof that the producer's
  calculation is correct.
- Core read Q2 at `14:24:39.041–14:24:41.068 +08:00` through
  `RedisQ2ProjectionAdapter`: 5,226 expected and observed symbols, zero
  missing hashes, `DataStatus.READY`, `BEST_EFFORT`, projection hash
  `ef31c7bb4347ee1b3e0ed787f69b4d4aa5d52b92eee87e2c9dd65c9c8e8aa609`.
  No freshness cutoff was applied. Core's anchor fact builder produced 5,070
  `AVAILABLE` and 156 `MISSING` 09:25 facts; all were marked
  `late_execution=true`. `historical_available_at=UNKNOWN`.
- Comparing by symbol, all 4,585 positive prices present in both the frozen TD
  snapshot and later Q2 are exactly equal; there are zero mismatches. Later Q2
  has 485 positive `a25` values where the frozen TD price is unavailable. Of
  those 485, 480 are members of the frozen Redis anchor archive and 5 are not.
  The 23 TD-only symbols all have unavailable frozen prices.
- A separate pair of full Q2 reads at 14:13, each over 5,226 symbols, differed
  in 2,991 per-symbol hashes and in the projection hash. This confirms that a
  live Q2 scan is a moving, non-atomic observation.
- In the 14:17 sample, every one of the 5,070 Q2 rows with positive `a25` had
  a row-level `source_record_time_ms` later than the 09:25:06.154 freeze time.
  That is event/source time for the current Q2 row, not the time its `a25`
  field became visible in Redis.

## Interpretation

The current Q2 read is a later mutable cohort. It cannot be labeled the
09:25:06 frozen snapshot. Core can represent its per-symbol facts as late
observations, while the frozen TD/Redis values remain the evidence for the
original freeze. The 485 extra positive values are consistent with later state
or correction, but the available evidence does not prove whether they came
from late-arriving auction ticks, later Q2 updates, candidate-set differences,
or another producer detail. The five values outside the frozen candidate
archive need separate explanation before any membership rule is inferred.

`DataStatus.READY` describes the complete best-effort current Q2 projection
under the no-freshness-cutoff read. It does not prove historical availability
at 09:25. Likewise, exact Redis/TD field agreement does not prove Rabbit
arrival order or the historical visible cohort.

## Mainline alignment

```text
same-day frozen-vs-later-source comparison: PASS_WITH_LIMITS
Core later Q2 anchor facts: 5,070 AVAILABLE / 156 MISSING, late_execution=true
09:25 historical Redis availability: UNKNOWN
Rabbit arrival/member order: UNKNOWN
same-day full tick → t1-v2 → Core replay: NOT_RUN
TASK-008: PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE: UNPROVEN
M3_1_NORMAL: BLOCKED
TD_WRITE_HEALTH: UNPROVEN
```

Next discriminating step: run the existing real TD three-second replay path for
2026-09-29 around the 09:25:06 barrier into an isolated Redis validation
namespace, capture the exact t1-v2 Q2/auction outputs at the barrier, and feed
those frozen outputs to Core. Compare that immutable replay cohort with the
preserved live freeze and with a separately timestamped later-Q2 revision.
Keep the two cohorts distinct and report any unavailable fields without
inventing arrival times. Do not promote TASK-008 from this reconciliation.
