# TASK-008 — Q2/TD auction amount field parity (2026-09-30)

Date: 2026-10-01 (Asia/Shanghai)
State: bounded same-producer parity evidence; `TASK-008` remains
`PARTIAL_EVIDENCE` and opening acceptance remains `UNPROVEN`.

## Question

Do the real event-time replay's final Q2 amount/rest fields agree with the
retained 09:25 TD auction snapshot for the same date and exact t1-v2 release?

## Evidence

- Q2Frame input is the frozen output of a real TD replay for 2026-09-30,
  `[09:15:00,09:25:09)`, one SELECT per 3-second slice:
  `/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2frame.jsonl`
  SHA-256 `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`.
- TD comparison input is the retained SELECT-only 09:25 `auction_snapshot_v2`
  capture, observed at 09:25:10.040 and stable on the second read:
  `/home/exedev/validation/task008-live-freeze-20260930-0925/td_0925_observation_1.json`
  file SHA-256 `0fed873cecdb163996263ab5ad63547843acd1d1ef95b943a510ada3a038c545`;
  canonical row-set SHA-256
  `df07b95093d1e0b7a276c8e264693a5a0c899e8d2ad373c51234396f643c719c`.
- Both artifacts were rechecked against their `sha256sums.txt` manifests.
- The exact replay binary is release `20260923_tdstop0945b`, SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.
  The pinned release writers serialize the same `state.auction` values to Q2
  (`am/br/ar`) and TD (`match_amt_yuan/rest_bid_amt_yuan/rest_ask_amt_yuan`).
  Source file hashes are recorded in the validation JSON.

## Method and result

For each symbol, retain the last Q2 update in recorded Q2Frame order, then
compare integer values with the matching TD snapshot row. No tolerance or unit
conversion was applied. The Q2 and TD cohorts each contain 5,220 symbols.

| Q2 field | TD field | Equal | Different |
| --- | --- | ---: | ---: |
| `am` | `match_amt_yuan` | 5,220 | 0 |
| `br` | `rest_bid_amt_yuan` | 5,220 | 0 |
| `ar` | `rest_ask_amt_yuan` | 5,220 | 0 |

这 5,220 个真实最新 Q2 状态也逐个通过 Core `normalize_q2` 复核：规范化后的
`am/br/ar` 对 TD 仍各为 5,220/5,220 相同；`a25` 有 5,030 个
`PRESENT_VALUE`，与 TD 可用价格 5,030/5,030 一致，另 190 个
`MISSING` 与 TD `NULL` 一一对应；当前 `px` 为正 5,220/5,220。复核使用
Core HEAD `07b6ac226f1468406ed590fb3d1d98efeaf51c59` 的
`src/engine_core/q2.py`，SHA-256
`07309abc2ed79ebe300637e52007bccea2f3c07fbe30b28a0c9932c3af3f951d`。

The latest per-symbol Q2 source timestamps range from 09:15:00 through
09:25:02; the TD snapshot timestamp is 09:25:06.026. The field parity is
reported as observed. The few seconds difference is not a failure condition
and does not establish Rabbit arrival or historical visibility time.

Detailed machine-readable result and provenance:
`/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2_td_auction_fields_20260930.json`.

## Interpretation and limits

This closes a previously untested field comparison for this date/release and
supports using the pinned `am/br/ar` Q2 values in Core development with the
documented units and meanings. It is same-producer projection/state parity,
not an independent market-value oracle. It does not prove Rabbit delivery or
arrival equivalence, historical `available_at`, other dates/releases,
`limit_state`, or NORMAL opening acceptance. It does not resolve the separate
2026-09-18 amount/rest differences.

The 2026-09-30 Q2Frame has no five-level arrays. The prior real-TD fifth-depth
candidate audit for 2026-09-18 and 2026-09-23 remains the relevant bounded
evidence for that feature:
`docs/work/handoffs/TD_RABBIT_STAGE_D_CANDIDATE_AUDIT_20260925.md`. Fifth-depth
shape is not itself a limit-state classifier.

No live connection or production write occurred during this follow-up. No
source implementation, service, task state, or production data was changed.
The current 742-test Core suite, compileall, and `git diff --check` had passed
before this handoff was added; this handoff contains no executable code.
