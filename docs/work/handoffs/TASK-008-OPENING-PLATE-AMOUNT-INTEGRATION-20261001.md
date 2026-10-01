# TASK-008 opening plate-amount replay integration

Date: 2026-10-01 (Asia/Shanghai)

## Result

```text
CORE_OPENING_PLATE_AMOUNT_IN_Q2FRAME_REPORT=PASS
REAL_SAME_DATE_LEGACY_PARITY=PASS_WITH_LIMITS
Q2FRAME_REPEAT_DETERMINISM=PASS
REPORT_EVIDENCE_JSON=PASS
TASK-008=IN_PROGRESS
```

This is a completed, bounded historical fact-slice check. It does not close
TASK-008, establish original Rabbit arrival-order equivalence, prove full
market coverage, or change M3-1 / production gates.

## What was integrated and exercised

`OpeningPlateAmountContextV1` is an explicit, date-pinned sidecar input to
`run_task008_q2frame_auction_engine_shadow.py`. Core does not query TD, Redis,
or current mappings to construct it. The runner validates its contract/hash
and trade date, then computes `OpeningPlateAmountSummaryV1` at the existing
`OPENING_0932` barrier from that replay's Q2 facts. The same context is used
for ordered and repeat runs and participates in the report's determinism
comparison.

The audit runner built the context from the exact-date frozen mapping and the
legacy auction projection, then ran the ordinary Core Q2Frame shadow path
through the opening barrier. It compared the integrated result with the
engine-next legacy observation on the same date and compared replay facts to
the pre-existing pinned Core report.

## Historical evidence

Final verified output directory:
`/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/`

The captured real TD SELECT input was reused by SHA-256, not queried again in
this integration run:

- Source: `market_data1.auction_snapshot_v2`, trade date `2026-09-29`.
- 15,669 rows: 5,223 each for tags `0920`, `0924`, and `0925`.
- Captured rows SHA-256:
  `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`.
- Same-date frozen mapping: 5,963 rows; effective time
  `2026-09-29T08:30:30.943852`; 5,155 auction symbols overlap it per tag.
- Exact-release t1-v2 Q2Frame SHA-256:
  `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`.
- Existing matching Core report SHA-256:
  `6fdc8485e9877b9f136148b594b1c77cac963868ab5fc27818d68bb2c11f180c`.

The integrated replay inventory found 5,223 symbols and 735 input frames.
It processed 734 frames / 418,759 updates through the `09:32:10` opening
evaluation; the next frame (`09:32:11`, 774 updates) was excluded by that
existing cutoff. There were no duplicate symbol occurrences within a frame.

A streamed inspection of the saved Q2Frame found that frame `09:30:00`
(`seq_no=604`) carried one update for each of 5,223 symbols: 2,435 had source
time `09:30:00`, while 2,788 retained earlier per-symbol source times between
`09:15:00` and `09:25:02`. By the `09:32:10` Core evaluation, 5,211 symbols
were `READY`, 12 were `PARTIAL/stale`, and none were missing; all 5,223 had an
`amt2m` value. This was not made a pass/fail gate: it is a concrete example of
the cross-section containing per-symbol times, including retained latest
values, rather than one universal tick timestamp.

These are source-event-time/frame-time facts, not Rabbit arrival latency or
proof of delivery order. The legacy Q2Frame observation adapter stamps its
normalized row with the frame logical time, while Core's input retains each
Q2 update's source time. The 10-plate parity comparison deliberately does not
compare per-symbol timestamp/freshness fields; therefore `STRICT_VALUE_MATCH`
means equality of the listed amount/cohort/status/symbol outputs, not complete
temporal-semantic equivalence.

## Comparison

- Legacy vs integrated Core: 10 selected plates × 16 fields, zero mismatches;
  classification `STRICT_VALUE_MATCH`, status `PASS_WITH_LIMITS`.
- Integrated report repeat comparison: deterministic across opening facts,
  auction evidence, frame/update counts, signal/reducer counts, final state,
  and virtual clock.
- Integrated opening facts hash equals the pre-existing pinned Core facts
  hash: `30c39fe184aa923abd24e7d837433bda17adce711dca72267ef57198176dadfe`.
- Final state hash:
  `59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27`.
- Artifact `sha256sums.txt` verified successfully.

`PASS_WITH_LIMITS` is deliberate: the ten-plate result is the legacy-selected
cohort, not a full-market assertion; the Q2Frame is t1-v2 event-time replay,
not the original Rabbit consume/processing sequence. Historical
`available_at` and Rabbit arrival order remain `UNKNOWN`. The small stale
cohort is preserved and reported, not converted to zero or used to block the
calculation.

The first completed integration output at
`/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T105700+0800/`
is preserved but superseded: Python read-only mappings had been serialized as
repr strings in its large Core report. `_write_json` now converts nested
`Mapping`/tuple values to JSON objects/arrays, a regression test covers nested
`MappingProxyType`, and the final rerun above contains `facts_by_symbol` as a
machine-readable object with 5,223 entries. Its artifact checksum manifest
verifies. The earlier failed module-import launch at
`/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T105606+0800/`
is also retained as a failed harness attempt, not replay evidence.

## Side effects and verification

This integration run reused local, hash-pinned evidence. It made no new TD or
Redis connection, performed no Rabbit consume/ACK, changed no service, and
emitted no effects. Files were written only under the validation output
directory. A first direct-script launch failed at module import before the
replay; that failure output was preserved separately, and the import path was
fixed before the successful run.

```text
Targeted tests: 43 passed
Serializer regression: 7 passed
Full suite after serializer correction: 784 passed, 3 upstream protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
```

The replay took roughly 35 minutes of wall time and completed. Elapsed time
was recorded as an observation, not used as a failure gate. Performance
optimization is separate from this correctness/parity result.

## Alignment / next step

This closes only the plate opening-amount integration slice. Keep TASK-008
`IN_PROGRESS`; do not alter the task board on the basis of this report alone.
Next, audit this integrated report against the original replay objective and
identify the next real-data-backed Core consumer slice. Preserve the limits
above; do not infer Rabbit arrival, full-market completeness, or live Redis
behavior from these files. `M3_1_NORMAL=BLOCKED` and
`TD_WRITE_HEALTH=UNPROVEN` are unchanged and outside this slice.
