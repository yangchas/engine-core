# TASK-008 live Q2 auction probe — 2026-10-08

## Scope

Read-only observation of the current Redis Q2 projection before and after the
09:15 auction start, followed by offline replay of the captured reads through
the existing Core adapter and probe Engine. This is a live-source ingestion
check, not historical replay equivalence or NORMAL opening acceptance.

The market-breadth quality fix was introduced in Core commit
`9b905db3ea07164f46240ef8b60f72b1ce74d02f` on
`codex/feature-session-engine-integration`. The 09:15 probe ran on that
revision; the 09:20/09:24/09:25+ probes ran on `8bb65fda9ed12345f32d3d2b9474cee54cb1aa18`
(the intervening commit only recorded the earlier probe handoff). The later
opening-fact diagnostic used Core commit `c774ba5`.
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

Further read-only captures followed the evolving live Q2 values:

| Observation | Read interval (Asia/Shanghai) | `a20` nonzero | `a24` nonzero | `a25` nonzero | Core status |
|---|---|---:|---:|---:|---|
| 09:20 | 09:20:16.576–09:20:18.066 | 3,282 | 0 | 0 | READY for captured active set |
| 09:24 | 09:24:18.635–09:24:20.078 | 3,713 | 4,307 | 0 | READY for captured active set |
| 09:25+ | 09:25:20.871–09:25:22.071 | 3,713 | 4,329 | 5,209 | READY for captured active set |

All three projections had 5,226 quotes and no configured age cutoff. The
09:25+ read's source-time distribution was not uniformly fresh: 5,209 `ts`
values were in the 09:25 minute; 17 were at 00:00 local time. Those same 17
had `a20=a24=a25=0`, `ls=0`, and `px=pc`. Core normalization represents the
17 zero `a25` values as `MISSING`; it represents the other 5,209 as
`PRESENT_VALUE`. This is a small missing cohort, not a reason to stop analysis.
The `READY` projection status did not apply a freshness threshold and must not
be read as “all 5,226 rows are fresh.”

The 09:25+ raw Q2 observation had latest `ts=09:25:02.000`, while the separate
Redis snapshot metadata recorded `tag=0925`, `n=5209`, and logical `ts=
09:25:06.025`. A read-only Redis metadata check around 09:28 found:

- `a2:20261008:0925`: hash exists, four fields; metadata tag/time/count match
  `0925` / `09:25:06.025` / `5209`.
- `market:auction:20261008:0925`: compatibility hash exists, three fields;
  same metadata.
- `market:auction:20261008:latest`: tag is `0925`, timestamp is
  `09:25:06.025`.
- `market:auction:anchor:20261008`: archive string exists (539,995 bytes); its
  full payload was not read.

A metadata-only Redis read was repeated at `2026-10-08T01:29:00.264220Z`
(`09:29:00.264220 Asia/Shanghai`) and completed at
`2026-10-08T01:29:00.269916Z`; it found the same keys and metadata.

This proves that by the 09:28 observation the producer had persisted a 0925
snapshot carrying the 09:25:06.025 logical trigger time. It does not prove the
key became externally visible at exactly 09:25:06; the first availability time
was not captured. In the checked writer source, `n` counts states with auction
time and at least one positive match/rest amount; the `top_amt`, `top_br`, and
`top_chg` fields are configured top-N lists, not the full market. The effective
top-N setting and full candidate membership were not read in this audit.
`a2` metadata `n` and Core's count of available `a25` values are both 5,209,
but this metadata-only check did not establish symbol-set equality. A later
read-only comparison against the full per-symbol archive is recorded below.

## 09:25 archive and Q2 symbol/field comparison

At 09:25:20.871–09:25:22.071 +08, the saved Q2 read capture contained all
5,226 active hashes. Its file SHA-256 is
`771ea94fe3c6a7f3444d70e13bb8dc815048ad5fa14f81530d386718732d9cfb`; the
canonical hash of the read transcript is
`863cc4c8f5c8d8711fcec9702212ed8167ad896ee4d1380ddcc35b40f6f8adb9`.

The Redis archive key `market:auction:anchor:20261008` was read with `GET` at
11:06:43.101–11:06:43.106 +08. It contained 5,209 symbol rows, 539,995 bytes,
with SHA-256
`c245862668184c6c2dabe6dab7f31e3e45e46ec77187a46df793bfd400352966`.
The read was read-only. The t1-v2 writer contract at
`b2aa169c3dddf5d5c7c452f4893dcc01848e358f` stores per-symbol
`change_pct`, `amount`, and `bid_amount` in this archive. They correspond to
Q2 `a25`/`pc`-derived change, `am`, and `br` respectively; `change_pct` uses
the producer's integer basis-point calculation and six-decimal serialization.

Results:

- Q2 `a25 > 0` symbols: 5,209; archive symbols: 5,209; intersection: 5,209;
  either-side-only symbols: 0.
- Archive `amount` versus Q2 `am`: 5,209/5,209 equal.
- Archive `bid_amount` versus Q2 `br`: 5,209/5,209 equal.
- Archive `change_pct` versus the producer formula applied to captured Q2
  `a25` and `pc`: 5,209/5,209 equal at six decimal places.

This closes symbol-membership and these three shared-field checks for the
saved Q2 capture and producer archive. The archive does not store the raw
anchor price, so it cannot establish direct `a25` price parity. The Q2 capture
was taken after the 09:25:06.025 logical trigger and is non-atomic across
symbols; the later archive read proves neither the key's first visibility
time nor Rabbit arrival order. The captured active set still does not prove
authoritative full-market coverage.

## Timestamp and auction-trigger source audit

The checked t1-v2 source at commit
`b2aa169c3dddf5d5c7c452f4893dcc01848e358f` separates event and envelope time:

- Protobuf `DataRecord.tss` maps to `SourceTickRecord.tss`, then
  `RawTick.ts_ms`, then Q2 `ts`.
- Rabbit outer `view.header.timestamp` is stored separately in
  `TickBatch.wall_ts_ms`.
- Therefore Q2 `ts` is the record/tick event timestamp, not the outer Rabbit
  header timestamp. The checked code does not prove whether that outer header
  timestamp means publisher-send time or broker-arrival time.
- `AuctionCalculator` captures the a25 candidate from tick timestamps in
  `[09:25:00, 09:25:20]`; `SnapshotTrigger` emits the a25 snapshot when its
  logical time reaches truncated second 09:25:06. These are separate clocks.

The live observation is consistent with that separation: Q2 source timestamps
for the 5,209 available a25 values were in the 09:25 minute (latest observed
09:25:02), while the stored snapshot metadata carries 09:25:06.025. This does
not reconstruct historical first-visibility or Rabbit arrival order.

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
- 09:20 capture: `/home/exedev/validation/task008-live-q2-0920-20261008T092012+0800-read-capture.json`
- 09:20 capture SHA-256: `4ba921ad451acec87cac67c9f68065e4cb762f4f2ec2193798ef0d445cf94748`
- 09:20 offline replay: `/home/exedev/validation/task008-live-q2-0920-replay-20261008T092012+0800.json`
- 09:20 replay report SHA-256: `e276997a6c920b49cb65493297d6fea6d59549311b588fdb49b6aaf81170b0cd`
- 09:24 capture: `/home/exedev/validation/task008-live-q2-0924-20261008T092410+0800-read-capture.json`
- 09:24 capture SHA-256: `6488bf924a6b1003ad768a703b5fc5f81bfab95a699dc2066ba0f1be4f6284bf`
- 09:24 offline replay: `/home/exedev/validation/task008-live-q2-0924-replay-20261008T092410+0800.json`
- 09:24 replay report SHA-256: `81aeeb800bf08c91dfa0c6faa8bbc75750a14bd2f1ccc3b3e382606a7ccab1a0`
- 09:25+ capture: `/home/exedev/validation/task008-live-q2-0925-plus-20261008T092515+0800-read-capture.json`
- 09:25+ capture SHA-256: `771ea94fe3c6a7f3444d70e13bb8dc815048ad5fa14f81530d386718732d9cfb`
- 09:25+ offline replay: `/home/exedev/validation/task008-live-q2-0925-plus-replay-20261008T092515+0800.json`
- 09:25+ replay report SHA-256: `f67f4cd2af1411f78565d849cd8391dc77d1f783085dedabf7dfe7ad4318a278`

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

## Feature-specific opening fact diagnostic

The frozen 09:25+ real Redis capture was then loaded offline through
`RedisQ2ProjectionAdapter`, reduced to one Core `EngineSnapshot`, and passed to
`OpeningShadowStrategy` for each of 5,225 equity symbols. This directly checks
the strategy's fact projection on real Q2 input; it is not a complete
`DeterministicEngine` session or NORMAL acceptance.

- All 5,225 opening change facts were numerically available and scoped
  `fact_status=READY`.
- The new per-symbol trace field reports
  `freshness_assessment=UNASSESSED` for all 5,225 because the capture supplied
  no freshness policy. It also exposes
  `source_time_age_ms_at_observation`; freshness is not inferred from a valid
  timestamp.
- Seventeen symbols had source time more than one hour before observation and
  `a25=MISSING`; 5,208 equity symbols had `a25=PRESENT_VALUE` and source age at
  most one hour. The one-hour split is descriptive only, not a gate.
- A regression confirms a precomputed stale error remains `STALE`, while a
  future source timestamp is `INVALID`/`PARTIAL` even if an adapter omitted its
  precomputed `future_ts` error. Numeric facts remain visible with their
  quality status; no global stop was added.

Commit `c774ba5` adds the source-age/freshness trace and regressions. Full suite
after this change: `887 passed`; compileall and diff-check passed. The 3
protobuf dependency deprecation warnings are non-failing.

## Status

```text
LIVE_Q2_0915_INGESTION=PASS_WITH_LIMITS
LIVE_Q2_0920_0924_0925_CAPTURE_REPLAY=PASS_WITH_LIMITS
OPENING_FACT_PROJECTION_ON_CAPTURE=PASS_WITH_LIMITS
LIVE_0925_ARCHIVE_Q2_SYMBOL_FIELD_RECONCILIATION=PASS_WITH_LIMITS
SAME_CAPTURE_CORE_DETERMINISM=PASS
CAPTURED_ACTIVE_SET_FULL_MARKET_COVERAGE=UNPROVEN
RABBIT_ARRIVAL_ORDER=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
```

No Redis/TD write, Rabbit operation, service restart, deployment, or
production-directory modification was performed. The feature-specific Core
opening fact projection has now been exercised on the captured Q2 cohort. The
symbol-level archive comparison now matches for the captured candidate cohort
and the three shared fields listed above. Direct price parity, first key
visibility, authoritative universe coverage, and NORMAL acceptance remain
unproven. Keep TASK-008 partial; do not impose a seconds-only rejection gate.
