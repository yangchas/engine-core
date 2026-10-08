# TASK-008 anchor-row isolation — 2026-10-08

## Finding

`AuctionTimeline.observe()` rejected the entire anchor observation when one
member was malformed. Duplicate normalized symbols could also be silently
resolved by input order, and a non-integer source timestamp could raise while
building time bounds. These are per-member quality problems; they should not
discard the other usable anchor facts.

## Repair

- Malformed rows are skipped and recorded as stable anomaly codes.
- Rows for a symbol with a normalization collision are all excluded, so neither
  input order nor “last row wins” can manufacture a selected anchor.
- A bad source timestamp is excluded from source-time bounds but does not
  invalidate that symbol's otherwise valid anchor value.
- Expected symbols whose rows were rejected remain unavailable/partial; other
  valid symbols continue through the same observation.
- Anomaly diagnostics appear only when present, preserving the clean bundle
  shape and content hash contract.

This is a per-row degradation path, not a new run-level acceptance gate.
Invalid top-level contracts, date/schema violations, and unsafe side effects
remain hard failures.

## Verification

- RED: the malformed-row case raised `ValueError`; duplicate rows were
  previously accepted as `READY` using last-row-wins behavior.
- GREEN: `tests/test_auction_timeline.py`: 35 passed; combined timeline,
  recovery, and runner tests: 58 passed.
- Full Core suite: `905 passed`, with three protobuf/upb deprecation warnings.
- `compileall`: PASS; `git diff --check`: PASS.
- Clean real-source check: the hash-pinned 2026-09-30 t1-v2 Q2Frame used by
  `tests/fixtures/q2/q2frame_0925_real_limit_states_20260930.json` has source
  SHA-256 `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`.
  Its 14 captured 09:25 anchor rows produced 14 available / 0 missing,
  `READY`, and zero input anomalies. This is a clean captured-cohort regression
  check, not a full-market or live-source claim.
- The malformed rows in tests are fault injection only; they are not presented
  as real market observations.

## Boundary and alignment

No live Redis, TDengine, or RabbitMQ access; no writes, ACKs, service changes,
deployments, or restarts occurred. No run-level market gate was added.

`TASK-008` remains `PARTIAL_EVIDENCE`; replay remains useful for development
with limits, and NORMAL opening acceptance remains `UNPROVEN`. This repair
closes only the anchor-observation row-isolation defect. Return to the existing
TASK-008 real-data comparison; do not promote the task or start strategy work
from this repair alone.
