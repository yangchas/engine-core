# TASK-008 — field-level anchor delta shadow

Date: 2026-10-08 (Asia/Shanghai)

## Result

Added a parallel, fact-only `AnchorFieldDeltaFactV1` projection. The existing
`AnchorDeltaFactV1` remains unchanged as the legacy-compatible oracle. The new
projection calculates price, match-amount, resting-bid, resting-ask, and book
pressure deltas independently, with per-field `AVAILABLE`, `MISSING`,
`UNKNOWN`, or `INVALID` status. It emits no direction or strategy conclusion.

```text
FIELD_DELTA_CONTRACT=PASS
LEGACY_ORACLE_UNCHANGED=PASS
REAL_20260930_FIELD_RECONCILIATION=PASS_WITH_LIMITS
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Why this was added

The old all-fields-valid helper correctly preserves its legacy directional
classification, but it returns the entire record as `unavailable` if price is
missing. A same-date real 2026-09-30 capture showed 190 such 0925 rows with
independently usable match-amount and resting-book values. Discarding those
numeric facts is unnecessarily coarse for Core fact development; treating
them as directional pressure would be an unsupported inference. The new
projection preserves the independent deltas without changing the old result.

## Real-data comparison

The reproducible read-only audit is:

```bash
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/audit_task008_anchor_field_delta_realdata.py
```

Pinned local inputs:

| Artifact | SHA-256 |
|---|---|
| 2026-09-30 TD auction capture | `162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d` |
| Current Core same-date replay report | `d0f7adbe5ba46b7aca3b26535ff2ec39461f97312f4547b42f49d58974c2cfe7` |

Observed results:

- 0924/0925 rows: 5,215 / 5,220; 5,220 symbol pairs; no duplicate
  tag/symbol rows.
- Core's 190 missing 0925 anchor-price symbols exactly equal the 190 TD rows
  whose `px_milli` is NULL. All 190 have available amount, bid, ask, and
  derived book-pressure deltas; no zero-fill was used.
- Within those 190 symbols, amount delta is nonzero for 179; bid delta for
  116; ask delta for 105; book-pressure delta for 178.
- The legacy helper still reports `resolved=3,115`, `unresolved=19`, and
  `unavailable=2,086`. For the 3,134 complete legacy tuples, all five numeric
  deltas match the new projection; there are zero mismatches.
- The same current Core report still produced 5,213 `READY` and 7 `PARTIAL`
  independent 09:32 opening facts. The incomplete anchor field did not stop
  unrelated opening facts.

The exact field-fact-set hash is
`457232dfe199d35fea5dd8fc63e90666ea1e4dcdc9188601196c6f27c6c7ecb6`.
This is a same-date content comparison over sealed artifacts, not proof of
historical `available_at` or live visibility.

## Verification

```text
targeted anchor-delta tests: 24 passed
full Core suite: 850 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
Core import-boundary scan for Redis/TD/Rabbit/network clients: no matches
```

The audit reads only the pinned local validation artifacts. It does not
connect to Redis, TDengine, RabbitMQ, Wencai, or a production service. It does
not write data, execute a recovery provider, or run effects.

## Alignment and next use

This is a parallel descriptive fact projection, not a replacement for the
legacy direction formula and not yet wired into a strategy or production
consumer. In particular, a missing price keeps price-derived direction
unknown even when amount/book deltas are available. A later consumer must name
the fields and source layer it actually uses; do not relabel book pressure as
auction directional pressure.

Historical tool-path note (2026-10-09):
`examples/audit_task008_anchor_field_delta_realdata.py` was later moved out of
the Core repository into
`/home/exedev/validation/task008-tools-archive-20261009T1355+0800/`. The
archive manifest records its checksum and restoration path; this
contemporaneous report describes the helper location at the time of that run.

The worktree already contained unrelated changes and remains uncommitted; no
blanket staging or commit was performed. TASK-008 remains partial. Recovery
provider execution, same-time live availability, Rabbit arrival, and NORMAL
opening acceptance remain unproven.
