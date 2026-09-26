# TASK-008 Phase 3/4 — real same-symbol-per-slice A/B

Date: 2026-09-26 (Asia/Shanghai)
Audit result: `PASS_WITH_SEMANTIC_DIFFERENCES` for the bounded 2026-09-18
window; default filtering is **not** accepted.

## Finding

The test compared two modes of the same t1-v2 binary over real TD
`market_data1.stock_tick_v2`, 2026-09-18 `[09:15:00,09:40:00)`, with 500
sequential three-second SELECT slices:

- A preserved all `1,224,811` source rows.
- B opted into latest event-time row(s) per symbol per slice; it processed
  `1,222,083`, explicitly accounting for `2,728` dropped rows, with zero tied
  maximum-time groups and zero source rejects.

B is not a transparent optimization. It changed 14 final raw Q2 symbol hashes
(`mn` for 7, `mx` for 7) and changed Q2 snapshots at 09:20, 09:24, 09:25:06,
and 09:26. The Core read-only `TOP_AMOUNT` auction projection changed at 09:20
but matched at 09:24/09:25 for this date/window. Details, hashes, Redis fields,
and exact summary deltas are in
`/home/exedev/validation/task008-dedup-20260926T210044+0800/dedup_ab_report.md`.

Therefore: **keep every TD row going through the shared t1-v2 calculation path
by default**. A three-second TD slice is not proof of one Rabbit `DataBatch`,
and event-time order is not Rabbit arrival order. If a future feature wants a
latest-per-symbol snapshot, evaluate it as a separate snapshot contract; do
not discard intermediate source events before t1-v2 computes them.

## Repeatability and checks

The all-row current build was replayed to isolated Redis DB10 and DB11. Q2,
A2, legacy auction, and anchor outputs matched; only M2 `redis_bytes` runtime
telemetry differed. The all-row and B barrier Q2Frame artifacts each replayed
through Core with `deterministic=true`, four barrier frames, 5,221 symbols,
and 20,884 updates. Core Redis adapters read both variants; Q2 projection hash
equality is limited to Core's canonical fields and does not cover raw-only
`mn/mx` fields.

Verification:

- t1-v2 full-dependency build and self-test: PASS.
- Core full suite: `705 passed`; compileall and `git diff --check`: PASS.
- `td_sql=0`, `ack=0`; Redis writes only to isolated validation DB6/DB10/DB11.
- DB0 run-prefix hits: zero. `engine-next` and `t1-v2-live` remained active,
  `NRestarts=0`; no service was restarted or deployed.

## Limits / alignment

- Current t1-v2 summary has aggregate counters, not a current per-frame
  row-count/digest manifest. An earlier captured real SELECT inventory has 500
  frames/98 empty frames, but that is not a per-frame attestation for these
  direct replay runs.
- Rabbit arrival order, delivery grouping, publisher timestamp assignment,
  and historical `available_at` remain `UNKNOWN`.
- 5,221 symbols is observed t1-v2 membership, not an independently proven
  full-market denominator.
- `TASK-008` remains `PARTIAL_EVIDENCE`; `M3_1_NORMAL=BLOCKED` and
  `TD_WRITE_HEALTH=UNPROVEN` are unchanged.
- No successor phase/task was started. Local t1-v2 experiment code has not been
  pushed, merged, or deployed. Core documentation changes do not alter the
  task-board structure.

The ECC `production-audit` and `cpp-testing` lenses were applied: source/sink
boundaries and DB isolation were audited, and the experimental policy has
focused tests for defaults, live-mode rejection, latest-time selection, ties,
and empty slices.
