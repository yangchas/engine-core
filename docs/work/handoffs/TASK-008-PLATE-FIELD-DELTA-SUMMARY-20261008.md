# TASK-008 — plate-level field delta summary

Date: 2026-10-08 (Asia/Shanghai)\
Base HEAD: `a18654e10af663c48dc0571c5baa6c457baebaf0`

## Scope and implementation

Added `OpeningPlateFieldDeltaSummaryV1` as a parallel descriptive consumer of
`AnchorFieldDeltaFactV1`. It independently aggregates `amount_yuan`,
`rest_bid_yuan`, `rest_ask_yuan`, and `book_pressure_yuan` per frozen plate
mapping. Price deltas are deliberately not summed across unrelated securities.

Each field carries its own expected, observed, available, missing, unknown,
invalid, and missing-symbol counts; usable-value coverage; zero count; sum;
and `available` / `partial` / `unavailable` status. A valid zero remains an
observation. A missing field fact is not filled with zero. The scope is always
`FROZEN_MAPPING_ONLY_NOT_FULL_MARKET`, full-market coverage remains
`UNPROVEN`, and the result remains `FACT_ONLY`. No direction, net-flow, or
strategy claim is emitted. The existing legacy-compatible directional
pressure summary remains a separate path and was not replaced.

Implementation is in `src/engine_core/opening.py`, with public exports in
`src/engine_core/__init__.py`, unit coverage in `tests/test_opening.py`, and a
reproducible file-only audit in
`examples/audit_task008_anchor_field_delta_plate_realdata.py`.

## Real-data verification

Command:

```bash
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/audit_task008_anchor_field_delta_plate_realdata.py
```

Pinned same-date inputs from 2026-09-29:

| Input | SHA-256 |
|---|---|
| TD 0924/0925 capture | `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b` |
| Frozen stock-to-plate mapping | `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b` |

Observed:

- TD rows: 5,223 at each anchor; 5,223 source symbols.
- Frozen mapping: 5,963 symbols across 450 plates.
- Mapping/capture overlap: 5,155; mapping-only: 808; capture-only: 68.
- All 5,155 overlapping symbols have both anchor rows; field aggregate reports
  5,155 available deltas and 808 missing mapped symbols for each of the four
  aggregated fields.
- Independent recomputation directly from the captured raw TD rows matched
  every per-plate field value, count, coverage, zero count, and status:
  `independent_raw_row_parity=PASS`, 0 mismatches.
- Reversing fact and mapping insertion order retained content hash
  `0a0a8eb5a12dd74f0eb258625f302d90e88722ae9f51cbb30e79ff6a53143457`.
- For each field, 376 plate summaries were `available`, 64 `partial`, and 10
  `unavailable`.

This is a reproducible calculation over sealed same-date SELECT output and a
same-date frozen mapping, not a fresh database read. The missing 808 mapping
members are retained in denominators; this audit does not decide why they are
absent from the captured source cohort. It does not prove historical
availability, live Redis visibility, TD completeness, or full-market
coverage. Sums remain descriptive aggregates, not a claim of capital flow or
strategy direction.

## Verification and boundary

```text
targeted new tests: 3 passed
full Core suite: 853 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
Core opening/anchor modules: no Redis, TD, Rabbit, or network-client imports
production side effects: NONE_OBSERVED
```

No Redis, TDengine, RabbitMQ, Wencai, service, or effect path was accessed in
this step. The repository was already dirty; changes remain uncommitted and no
task-board state was changed. This stage does not close TASK-008 or establish
NORMAL opening acceptance. M3-1 remains `BLOCKED`; `TD_WRITE_HEALTH` remains
`UNPROVEN`.

## Alignment

This increment advances the Core fact path from per-symbol field deltas to a
plate-level consumer without changing the legacy pressure formula or
loosening production gates. The next alignment review should decide whether
this summary is ready to be attached as a separate field-fact section to the
current Core opening replay evidence. Do not merge it into directional
pressure fields or treat this same-date retrospective audit as proof of live
readiness.
