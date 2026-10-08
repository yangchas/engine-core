# TASK-008 cohort soft-failure repair — 2026-10-08

## Finding

The opening amount, limit-state, and anchor-to-opening transition summaries
raised `ValueError` if a single input symbol was outside the caller-declared
`expected_symbols`. In the TASK-008 report path, that could abort report
assembly even when the expected cohort had usable facts. Missing expected
symbols were already represented as partial coverage, so treating one extra
row as a whole-run hard failure was inconsistent with the intended resilient
fact-processing behavior.

## Change

The three summaries now:

- aggregate only symbols in the explicitly declared cohort;
- preserve expected-but-unobserved counts using the existing coverage fields;
- exclude out-of-scope symbols from denominators and values;
- when extras are present, report `out_of_scope_symbol_count` and sorted
  `out_of_scope_symbols` so they are visible and auditable rather than silently
  accepted. Clean-cohort output shape and content hashes remain unchanged.

This is not a blanket relaxation. Invalid cohort labels, malformed contracts,
date/schema violations, and unsafe side effects remain hard errors. The change
does not alter the Engine input stream or claim out-of-scope rows are valid.

## Verification

- RED: three focused regression cases failed on the prior behavior because
  each raised on an out-of-cohort symbol.
- GREEN: focused regression cases: `3 passed`.
- Core full suite: `889 passed`; three protobuf/upb deprecation warnings.
- `compileall`: PASS.
- `git diff --check`: PASS.
- Re-ran the file-only limit-state/amount audit on hash-pinned real
  2026-09-29/30 TD→t1-v2→Q2Frame evidence. Result:
  `PASS_WITH_LIMITS`; observed cohort 5,223/5,223 with zero out-of-scope
  symbols, fresh cohort 5,211/5,211 with zero out-of-scope symbols, and all
  seven producer/Core comparison checks true. The clean-cohort report did not
  gain optional out-of-scope fields. This consumes retained evidence; it is
  not a new live Redis/TD/Rabbit read or a repeated replay.
- Audit output:
  `/home/exedev/validation/task008-robust-cohort-summary-20261008/audit-v2.json`
  SHA-256: `378fc5198016e05a90357bf85cf1af590d3c5e78b56a6b55fa2c789d9842f415`.
- Current worktree source SHA-256: `opening.py`
  `e5c84e735c8d69dbfcf64571e7bb4e105158ec01160c8cd135b481287b7628c0`;
  `test_opening.py`
  `fa92fa750721cb0f9228a8465c464c3305f4a11019eb4d8e21fe197ad1a387e4`.

## Boundary and status

No production services or source databases were accessed by this repair; no
Redis/TD writes, Rabbit operations, deployments, or restarts occurred. The
real-data verification used already-frozen evidence only. The repair is in the
current dirty Core worktree and is not independently committed yet; do not
interpret its test result as evidence that every unrelated dirty change is
committed or releasable.

`TASK-008=PARTIAL_EVIDENCE` remains unchanged. This closes only a local
report-assembly hard-stop for out-of-cohort rows; it does not establish
full-market coverage, NORMAL opening acceptance, Rabbit arrival order, or
historical `available_at`.

## Follow-up — absent anchor remains UNKNOWN — 2026-10-08

### Finding and repair

`build_opening_transition_summary` previously defaulted an absent 09:25 anchor
mapping row to `MISSING`. Absence of a record does not prove the producer
observed the symbol and explicitly declared its anchor missing. The summary
now defaults only absent records to `UNKNOWN`; a source record explicitly
marked `MISSING` remains `MISSING`. Missing anchor facts make that symbol's
transition unavailable/partial, but do not stop the remaining cohort.

### Evidence

- TDD: the focused transition-summary regression failed before the repair
  (`UNKNOWN` expected for absent anchor, `MISSING` observed), then passed.
- Contract test distinguishes an explicit `MISSING` anchor from both an
  absent anchor row with an opening quote and a symbol absent from both inputs
  but present in the expected cohort. The latter remains in the summary as
  unavailable rather than aborting the run.
- Recomputed the transition summary using current Core code from the pinned
  real 2026-09-30 Q2Frame and its same-date Core report. Q2Frame SHA-256:
  `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`;
  report SHA-256:
  `3d1000b522d960bd0081fbd657d907ffbda43dbb5db7ac6b3ae84e366359daa2`.
  The 5,220-symbol cohort had 5,220 explicit anchor rows: 5,030 `AVAILABLE`,
  190 `MISSING`, zero absent anchor records. Recomputed transition and
  per-symbol fact hashes both equal the pinned report; comparable/unavailable
  counts remain 5,030/190. Thus this correction does not change the real
  cohort's values or counts; it corrects only an absent-record edge case.
- Current worktree source SHA-256: `opening.py`
  `e5c84e735c8d69dbfcf64571e7bb4e105158ec01160c8cd135b481287b7628c0`;
  `test_opening.py`
  `fa92fa750721cb0f9228a8465c464c3305f4a11019eb4d8e21fe197ad1a387e4`.
- Full suite: `893 passed` (three protobuf/upb deprecation warnings);
  compileall and `git diff --check` pass.

### Alignment

This is a field-quality correction, not an acceptance gate. It does not alter
the 09:25/09:32 numeric facts for the pinned real cohort, and it does not claim
NORMAL opening acceptance, full-market authority, Rabbit arrival order, or
historical `available_at`. No live data source or production side effect was
involved in this follow-up.

## Follow-up — isolate plate field-delta row failures — 2026-10-08

### Finding and repair

`build_opening_plate_field_delta_summary` validated every supplied fact before
selecting the requested plate cohort. This meant an irrelevant malformed
out-of-cohort row could stop a fact-only report. A wrong-anchor row for one
selected symbol also raised for the entire aggregation. The summary now:

- selects the requested plate-symbol union before validating source rows;
- reports identifiable out-of-scope symbols without reading their row payloads;
- treats duplicate normalized keys and malformed/wrong-anchor selected facts
  as `INVALID` for that symbol across the field denominators;
- excludes invalid values while continuing the remaining cohort; and
- validates these optional diagnostics when the summary is wrapped in a
  replay context.

Outer mapping shape, requested date, and selected mapping structure remain
validated; this change does not relax those contracts or alter any Engine
input. It is per-row fault isolation, not a blanket acceptance gate.

### Verification

- RED/GREEN regression: selected wrong-anchor fact is `INVALID`; malformed
  rows outside the selected cohort do not abort the summary; context validation
  preserves and checks the diagnostics.
- Full Core suite: `894 passed`; compileall and `git diff --check` pass.
- Re-ran `examples/audit_task008_anchor_field_delta_plate_realdata.py` against
  its hash-pinned sealed 2026-09-29 TD rows and frozen plate mapping. Independent
  raw-row parity remains `PASS` with zero mismatches for all four fields.
  Counts and sums equal the prior pinned audit; 68 capture-only symbols are
  now represented in the out-of-scope diagnostics. The summary hash changed
  from `0a0a8eb5a12dd74f0eb258625f302d90e88722ae9f51cbb30e79ff6a53143457` to
  `c6e512bc6846732f597e7485b5cf0e96c40da7aa61069dcff8d243bbef75c737` because
  those diagnostics now participate in the content hash.
- Sealed input hashes: TD
  `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`; mapping
  `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b`.
- No live Redis/TD/Rabbit access, writes, service changes, or production side
  effects occurred.

### Alignment

This closes only a report-level cohort robustness defect. TASK-008 remains
`PARTIAL_EVIDENCE`; full-market coverage, NORMAL opening acceptance, Rabbit
arrival order, and historical `available_at` remain unproven.
