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
  `8c46e2d0dea3b2ddadeba9b5ec3dd27839509570f3b8215e715c407863478b09`;
  `test_opening.py`
  `4850cb04e68de5dadd80362ee976872789579c8d723dfdc2322fd6d65e89dbde`.

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
