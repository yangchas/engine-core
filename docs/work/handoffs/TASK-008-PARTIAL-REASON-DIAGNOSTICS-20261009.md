# TASK-008 Partial Reason Diagnostics Handoff — 2026-10-09

## Result

`PartialReasonDiagnosticsV1` is implemented as an opt-in, report-only addition.
It does not change auction facts, `StrategyResult`, Engine inputs, acceptance
status, or the existing `Task008Q2FrameSessionEngineShadowV15` contract version.

The first report from this work is superseded because its primary-reason
classification omitted the current 09:25 anchor and treated structural coverage
as a symbol-level cause. Keep it as historical evidence; use the corrected
report below for review:

```text
superseded:
/home/exedev/validation/task008-partial-reason-diagnostics-20261009T011817+0800/core_full_window_report.json

corrected:
/home/exedev/validation/task008-partial-reason-diagnostics-final-20261009T015518+0800/core_full_window_report.json
SHA-256:
8fdb34fe80f67babe54c337403ff12b8b45981ab32c37ffdc9e015ca761701e5
```

## Frozen replay evidence

- Trade date: `2026-09-23`.
- Main Q2Frame artifact SHA-256: `c6317dcf444ca4b54c36cba43713294ccdf04ba325c1e2abb603c29b67d5a1a4`.
- Barrier sidecar SHA-256: `91daad1c6ec2236796e25b6ce4e5fcf66a077d58c2d1159e3e8e3754b145cc58`.
- Ordered and repeat passes: deterministic; 500 frames, 1,209,672 updates,
  5,222 symbols; final state hash is identical:
  `7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d`.
- 09:20 and 09:24 remain `PENDING` for all 5,222 symbols; 09:25 remains
  `PARTIAL` for all 5,222 symbols.
- Corrected 09:25 diagnostics: 4,136 symbols have at least one absent auction
  anchor (co-occurrence only); 1,086 have all three anchors and comparable
  fields but remain structurally partial. Coverage and volume-semantics flags
  are explanatory structural/run-level flags, not stop conditions.
- Removing only the added diagnostics contract key and the ordered/repeat
  09:25 diagnostics objects makes the sorted corrected report exactly equal to
  the pre-diagnostics baseline (`diff` exit 0). Existing hashes and semantic
  report content therefore remain unchanged.

The input is a frozen artifact captured from the exact-release t1-v2 output.
This run read local validation files only; it did not freshly query Redis,
TDengine, or RabbitMQ and must not be represented as a new live-source test.
The report records production side effects as `NONE; local Q2Frame read and
in-memory Core Engine only`.

## Corrections and verification

- Diagnostics now inspect all three anchors: 09:20, 09:24, and 09:25.
- `COVERAGE_NOT_READY_STRUCTURAL` is not itself selected as a symbol-level
  primary cause. `STRUCTURAL_ONLY_COMPARABLE` is used only when all anchors are
  available and comparable fields exist.
- Volume-semantics uncertainty is read from the instantiated child strategies,
  rather than hard-coded.
- Unknown field quality remains unknown; comparison labels are not used to
  infer source-field missingness or causality.
- Targeted tests: `4 passed, 16 deselected`.
- Full suite: `937 passed`; `compileall` and `git diff --check` passed.
- Existing worktree contains other user changes. No unrelated files were
  reverted, staged, or committed.

## Read-only decision review and next step

The existing Claude Code session was reused for the final read-only audit. It
found no blocker in this diagnostics correction and directed work back to
`C.1 FRAME_GAP`; do not expand this diagnostics patch further.

C.1 guardrails:

- Keep date, schema, pinned SHA, and empty-artifact validation as hard failures.
- Consider soft continuation only for sequence gaps and mild timestamp
  regressions; record gap counts and affected positions/time spans.
- Do not invent a fixed gap threshold. Derive any stop boundary from the real
  artifact's frame distribution and preserve a clear abort for materially
  incomplete input.
- Verify the real 2026-09-23 artifact retains all existing hashes when it has
  no such gaps; add synthetic small-gap and large-gap tests, including proof
  that independent 09:25 evidence on both sides of a gap remains available.
- Keep C.1 changes separate from this diagnostics patch for review and
  traceability.
