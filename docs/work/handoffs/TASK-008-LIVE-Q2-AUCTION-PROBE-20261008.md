# TASK-008 live Q2 auction probe — 2026-10-08

## Scope

Read-only observation of the current Redis Q2 projection before and after the
09:15 auction start, followed by offline replay of the captured reads through
the existing Core adapter and probe Engine. This is a live-source ingestion
check, not historical replay equivalence or NORMAL opening acceptance.

Core revision: `9b905db3ea07164f46240ef8b60f72b1ce74d02f` on
`codex/feature-session-engine-integration`.
The read-only producer enum/writer source was checked at t1-v2 commit
`b2aa169c3dddf5d5c7c452f4893dcc01848e358f`; its worktree was clean.

## Source evidence

The pre-open capture completed at `2026-10-08T00:59:34.044Z`
(`08:59:34.044 Asia/Shanghai`). It read 5,226 active symbols. The hashes had no
`trade_date` field; validation derives the date from `ts`: 4,426 source times
were on 2026-09-30 and 800 on 2026-10-08. At that observation, source age ranged
from about 9 hours to 8.37 days. No freshness threshold was configured, so a
zero stale count would not have established freshness. This may be retained
pre-open cache; its cause is not established.

The 09:15 capture read started at `2026-10-08T01:15:18.117Z` and completed at
`2026-10-08T01:15:19.487Z` (`09:15:18.117–09:15:19.487 Asia/Shanghai`). It used
one `SMEMBERS` and 5,226 `HGETALL` operations. Results:

- 5,226/5,226 active members had a nonempty hash; Core projection status was
  `READY` for this captured active set, with no field errors.
- Source-time age ranged from 1.487 seconds to 33.319 seconds; median was
  10.487 seconds. No future source times were observed. No freshness cutoff was
  applied; this is a distribution, not a freshness pass threshold.
- All 5,226 rows had `ph=1`. The pinned t1-v2 enum maps `1` to
  `MarketPhase::Auction`; `redis_v2_writer.cpp` serializes that enum into `ph`.
- `a20`, `a24`, and `a25` were present as explicit zero on all 5,226 rows at
  this observation. This records the wire values only; it does not establish
  anchor finality or interpret zero as a completed auction fact.
- Core market cross-section over the captured cohort: 2,059 up, 1,483 down,
  1,683 flat, 0 unknown, and 1 excluded non-equity symbol.

## Determinism and artifacts

The online probe ran the same captured projection twice through one Engine per
run; both snapshot hashes were
`c9dbe851feebeb84490fc3f642478dc597925481cda0b87bf00c811359a507b1`, and both
probe hashes were
`cf2effbb6ea4f2894520496fc3460beae599e6df0559845a41794048096ecb06`. Offline
replay of the exact capture reproduced the projection, snapshot, and probe
hashes.

- Pre-open report: `/home/exedev/validation/task008-live-q2-preopen-20261008T085932+0800.json`
- Pre-open capture SHA-256: `fc24e0715e0290d3be12d84b1fc100f3d1bd6e3e751e22aebc666a6dd65ac78c`
- 09:15 report: `/home/exedev/validation/task008-live-q2-open-20261008T091510+0800.json`
- 09:15 capture: `/home/exedev/validation/task008-live-q2-open-20261008T091510+0800-read-capture.json`
- 09:15 capture SHA-256: `b179ce25ec62ba138756bbd9c8874fad1e0b403b2dc5f6e6636b79943cb2491f`
- Offline replay report: `/home/exedev/validation/task008-live-q2-open-replay-20261008T091510+0800.json`
- 09:15 canonical input SHA-256: `6ff637cf4fd3f9a375ecabeca0932d28f35789b3ad16a7f04d79769246746ba9`

The read is non-atomic across symbols. `q2:active` membership does not prove
authoritative full-market coverage. `ts` is source-record time, not Redis
`available_at`, Rabbit arrival time, or historical visibility.

## Narrow reducer repair

The pre-open capture exposed that the reducer counted cross-date quotes in
current market breadth despite their `trade_date` quality errors. Commit
`9b905db` retains the raw symbol observations and projection, but classifies
quotes carrying invalid time/price quality as `unknown` for market breadth.
This changes no global readiness gate. The pre-open capture now reports 528 up,
235 down, 37 flat, and 4,425 unknown among 5,225 equity observations; the 800
same-date rows remain directionally counted because freshness policy is
unset, so they are not thereby proven fresh.

## Status

```text
LIVE_Q2_0915_INGESTION=PASS_WITH_LIMITS
SAME_CAPTURE_CORE_DETERMINISM=PASS
CAPTURED_ACTIVE_SET_FULL_MARKET_COVERAGE=UNPROVEN
RABBIT_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
```

No Redis/TD write, Rabbit operation, service restart, or production code change
was performed. The next useful live check is a read-only capture around the
09:25 auction freeze, retaining the actual observation interval and source
times; seconds-level differences are evidence, not a hard rejection gate.
