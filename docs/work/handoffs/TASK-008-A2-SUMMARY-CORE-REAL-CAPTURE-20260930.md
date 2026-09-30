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
