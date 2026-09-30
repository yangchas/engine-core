# TASK-008 — Core normalization of real 09:25 A2 summary

Date: 2026-10-01 (Asia/Shanghai)
Status: bounded real-payload adapter verification; `TASK-008=PARTIAL_EVIDENCE`.

## Evidence and method

- Input is the frozen Redis observation from
  `/home/exedev/validation/task008-live-freeze-20260930-0925/capture_summary.json`,
  SHA-256 `90a0f5b83aaabc69c762936d4e0454b84b89192e9b72549c5deb0be41d1fe2dc`.
- It is the `summary` field of Redis hash
  `market:auction:20260930:0925`, read at
  `2026-09-30T09:25:20.028057+08:00`; the summary itself records snapshot time
  `09:25:06.026`, a 14.002-second difference. This observed lag is recorded,
  not used as a failure gate, and does not establish when the value first
  became visible.
- The repository fixture was compared field-for-field against the captured
  Redis payload. `normalize_auction_market_summary()` was then run on that
  fixture using the Core package and the captured source/observation metadata.
- Core normalizer source SHA-256:
  `b703228cda742052ea589d1a1e9f2b96b7ef5a3d7469765309ecc67ee5dcc2d7`.
- Producer provenance is the exact release already used by the same-day replay:
  binary SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`;
  `redis_v2_writer.cpp` SHA-256
  `9baa218adb6f00cf7ae85929659c74ff9d01ce4882bc9180cc1ef46d9e586940`.

## Result

Core produced `status=READY`, with no missing or invalid fields:

| Canonical fact | Value |
| --- | ---: |
| stock_count | 5,210 |
| valid_stock_count | 5,030 |
| unavailable_stock_count | 180 |
| positive / negative / flat | 3,534 / 785 / 711 |
| auction_amount_yuan | 11,881,094,371 |
| limit_up / limit_down | 10 / 4 |
| limit_up_seal_amount_yuan | 15,056,893 |

Content hash:
`8ed6214126a134bd039f1a06779bdc3c2ef054e74a931735811da812f58d103c`.

The test first failed because the 2026-09-30 source fixture did not exist, then
passed after adding the frozen real payload. This verifies Core's existing A2
summary input normalization and field mapping; it does **not** calculate A2
market statistics from per-symbol Q2 rows. The exact-release replay-vs-live
Redis A2 summary parity is independently recorded in
`producer_anchor_parity_20260930.json`; this test does not broaden that result.

## Limits and side effects

This is one date, one snapshot, and one producer release. It does not prove
historical Redis availability, Rabbit arrival order, universal market
completeness, strategy correctness, or NORMAL opening acceptance. The
5,210-symbol A2 cohort is the producer's emitted cohort, not an asserted
full-market universe. No live Redis/TD/Rabbit connection or production write
was made during this follow-up; no service, producer, or task state changed.

Regression:
`tests/test_market_summary.py::test_live_20260930_0925_summary_maps_to_canonical_a2_fact`.

Verification on the resulting worktree: full Core suite `743 passed` (three
protobuf deprecation warnings), `compileall` passed, and `git diff --check`
passed. No business implementation or production path changed.

## Same-day Q2/A2 cohort reconciliation

The frozen Q2Frame was streamed in recorded order, retaining only the latest
row per symbol, then each row was passed through Core `normalize_q2`. Its 5,220
symbols were compared with the actual 5,210-member Redis 0925 anchor and the
same-date Core engine shadow's 0925 anchor-fact status counts. Inputs and
derived counts are recorded in
`/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2_a2_core_cohort_reconciliation.json`.

| Cohort / fact | Present | Missing or unavailable | Total |
| --- | ---: | ---: | ---: |
| Core Q2 `a25`, all replay symbols | 5,030 | 190 | 5,220 |
| Core Q2 `a25`, Redis A2 members only | 5,030 | 180 | 5,210 |
| Captured Redis A2 `change_pct` | 5,030 | 180 NULL | 5,210 |
| Captured A2 summary valid / unavailable | 5,030 | 180 | 5,210 |

All 5,210 Redis A2 members exist in the Q2 set; none exist only in Redis.
Within that shared producer cohort, Core's `a25` presence status matches
Redis's `change_pct` present/NULL status for all symbols. The 10 Q2-only rows
are `000016`, `300082`, `300716`, `300753`, `300901`, `600293`, `601059`,
`601198`, `603183`, and `688496`; each has `a25=0` and `am=br=ar=0`. The pinned
producer source builds A2 candidates only when at least one of those auction
amounts is positive (and an auction timestamp exists), so their absence from
the A2 candidate cohort is consistent with the observed amounts. Q2 `ts` is
not treated as the producer's separate auction timestamp.

This reconciles the apparent `5220` versus `5210` count difference as two
different cohort scopes, not a failed 09:25 data read. It does not make either
cohort a claimed full-market denominator. The 14.002-second snapshot/read
difference remains observation metadata only, not a gate.

## Same-day numeric parity and limit-state edge cases

A follow-up compared the frozen Q2Frame, the Core 09:25 anchor facts, the
TD `auction_snapshot_v2` SELECT capture, and the actual Redis A2 anchor and
summary. The machine-readable results and source hashes are in
`/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2_core_anchor_numeric_parity_20260930.json`.

- The 5,220 Core 09:25 anchor values and availability states match Core's
  `normalize_q2(a25)` result for all 5,220 symbols.
- For the 5,030 available A25 prices, Core and TD `px_milli` match exactly;
  their other 190 A25 values are unavailable (TD encodes them as zero/null).
- Applying the pinned producer integer formula
  `trunc_toward_zero((a25_milli - pc_milli) * 10000 / pc_milli)` reproduces
  TD `chg_bp` for all 5,030 available prices. Redis `change_pct` also equals
  `TD chg_bp / 10000` for all 5,030 non-null Redis values; 180 are null.
- Re-deriving all 10 A2 summary fields over the captured 5,210 Redis A2
  members, using that same price-change formula and the release's Q2 `ls` / `am`
  / `br` fields, matches the captured Redis summary exactly. This closes the
  cohort-level arithmetic and mapping check; it is not an independent market
  oracle or an independent reconstruction of `ls` from original ticks.
- The captured cohort has 10 producer `ls=+1` and 4 `ls=-1` rows. Real cases
  include `600241` classified UP at `+995 bp` and `002285` classified DOWN at
  `-996 bp`. Do not infer limit state from a fixed +/-10% threshold. A compact
  fixture retains the full source Q2 rows and per-row hashes, and a regression
  test checks that Core preserves `ls` and the A25 values rather than
  reclassifying them.

The A2 payload timestamp is `09:25:06.026`. Redis observation 1 found the A2
hash absent at `09:25:08.192892`; observation 2 found it present at
`09:25:20.028057`. This bounds observed absence/presence across those reads,
but does not identify the exact write or first-availability time. The 14.002
second gap is recorded as evidence, not treated as failure: the intended
purpose is to verify the stored auction snapshot and its content, not impose a
subsecond storage deadline. Historical `available_at` remains UNKNOWN.

This follow-up uses frozen local evidence only; it made no live Redis/TD/Rabbit
connections or writes, and did not change a producer or service. It proves
same-day consistency for this release and observed cohorts only. The Redis A2
set (5,210) and replay Q2 set (5,220) are not asserted to be the full-market
universe, Rabbit delivery membership/order remains UNKNOWN, and
`NORMAL_OPENING_ACCEPTANCE` remains NOT EVALUATED.
