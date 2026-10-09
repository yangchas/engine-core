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

## Follow-up — isolate auction-pressure row failures — 2026-10-08

### Finding and repair

`build_opening_plate_auction_pressure_summary` previously checked that every
fact in the input was a mapping before limiting work to the selected plate
cohort. One malformed, unrelated row could therefore stop otherwise usable
FACT_ONLY output. It now:

- selects the symbol union of the requested plates before inspecting payloads;
- keeps malformed selected rows out of the sum and counts them as invalid for
  their symbol;
- reports sorted out-of-scope symbols without validating their payloads; and
- validates the optional diagnostics when a summary is wrapped in a context.

Trade-date, mapping-container, and selected-plate structure errors still fail
clearly. This isolates row-quality failures without weakening source or
business semantics.

### Verification

- Regression reproduced the previous `TypeError` and now confirms one invalid
  selected symbol does not prevent the other selected symbol from contributing.
- Full Core suite: `895 passed`; compileall and diff-check pass.
- File-only parity audit used hash-pinned 2026-09-29 TD rows, a frozen plate
  map, and hash-pinned engine-next release helper source. Strict per-symbol
  formula comparison: 5,223/5,223; plate pressure value mismatches: 0; status
  mismatches: 0; denominator mismatches: 8. Overall remains
  `VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE` because 91 selected mapping
  members are not present in the captured TD cohort. This is not live-source,
  producer-binary, or full production-assembly proof.
- Recomputed selected-cohort pressure summary reports 3,902 input facts
  outside the ten selected plates; they do not enter plate denominators or
  pressure values. Audit output:
  `/home/exedev/validation/task008-auction-pressure-row-robustness-20261008T115319+0800/`.
  `audit_summary.json` SHA-256:
  `9f2c3f2ca4decb501af60021ebc77ddb74e4e3ef12efdeb5fe1a996fd3b83fb6`;
  recomputed summary SHA-256:
  `dff981df009c54bebc8523a1aad6bdd56ef0c8b746e1e39576a53d72b4b93833`.
- TD capture SHA-256:
  `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`;
  frozen plate map SHA-256:
  `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b`.
- Audit declared TDengine, Redis, and RabbitMQ `NOT_CONNECTED`, with no
  production service effects. It read only retained files and pinned source.

### Alignment

The failure path now degrades by symbol, not by whole summary. Numerical
parity is preserved on the captured real cohort, while the denominator
difference remains visible and is not converted into a strict pass or a run
gate. TASK-008 remains `PARTIAL_EVIDENCE`.

## Follow-up — recovery duplicate and transition-row isolation — 2026-10-08

### Finding and repair

Recovery normalization previously selected the last row when different aliases
normalized to the same symbol, making the accepted value depend on source
iteration order. It now collapses identical normalized rows and quarantines a
conflicting symbol while retaining valid sibling recovery fills. Direct
row-list recovery also accepts a matching embedded `symbol` as identity
metadata when the existing primary cohort was keyed by symbol; an identity
which disagrees with the normalized key remains a hard error. The opening
transition summary now isolates a non-mapping anchor or opening row to that
symbol, records an invalid-row diagnostic, and continues the rest of the
cohort. A response which misdeclares one zero-valued anchor or omits one
declared filled symbol now leaves that symbol unavailable and keeps other
valid fills; an `APPLIED` envelope with no actual fills remains an error.

Follow-up audit found one uncovered encoding pair: the real Q2 adapter maps
`a25=0` to `None`, while a recovery response may return `0` for that member
beside valid sibling fills. The timeline now restores the primary `None`
rather than treating the default as a source rewrite; a zero-only member with
no primary row is excluded from the applied cohort. The regression also retries
the same result to check idempotency.

### Verification

- Focused recovery/timeline/opening tests: 103 passed.
- Full suite: 920 passed; `compileall` and `git diff --check` pass.
- The primary `None` comes from the hash-pinned fixture
  `tests/fixtures/q2/q2frame_null_a25_live_rows_20260930.json` (Q2Frame SHA-256
  `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`). The
  default-zero recovery response is a contract edge test, not a real Wencai
  response.
- Recalculation from the pinned real 2026-09-30 Q2Frame through 09:32:10
  reproduced the existing clean transition summary exactly: 5,030 comparable
  symbols, same summary hash. This validates the clean path against retained
  historical input; it is not live-source or runtime-timing proof.
- No Redis/TD/Rabbit connection or write, service action, producer change, or
  deployment occurred.

### Alignment

The repair degrades by symbol for malformed/ambiguous members, but still
rejects wrong symbol identity and recovery attempts that rewrite observed
primary facts. It adds no run-level completeness or performance gate.
TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening acceptance remains
`UNPROVEN`. Explicit feature-scoped work may continue; this state is not a
project-wide development stop.

## Follow-up — sparse recovery response completion — 2026-10-08

### Finding and repair

A plan-bound `APPLIED` recovery response can contain valid fills while omitting
unchanged members or old fields already observed in the primary cohort. The
timeline previously rejected that entire response. It now completes the
response from the saved primary rows for that exact base revision and emits
restoration anomaly codes. At this point in the work, rewrites of observed
values still rejected the whole response; the following audit follow-up
supersedes that behavior with per-symbol quarantine. Invalid identity,
unrequested fields/fills, and out-of-scope new facts remain hard errors. A new
member with no usable requested value is omitted with a diagnostic; recovery
does not promote completeness or change `FACT_ONLY` semantics.

### Verification and limits

- The primary `None`/source rows are from pinned fixture
  `tests/fixtures/q2/q2frame_null_a25_live_rows_20260930.json`, Q2Frame SHA-256
  `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`.
- Focused recovery/timeline/opening suite: 105 passed; full suite: 922 passed;
  compileall and `git diff --check` pass.
- Ordered/idempotent sparse response and hard-rejection cases are covered.
  The recovery response is a contract test, not captured Wencai output; no live
  Redis/TD/Rabbit source, production service, or write path was exercised.
- The prior 35-minute replay was not rerun because the patch changes only the
  recovery-result application seam. TASK-008 remains `PARTIAL_EVIDENCE` and
  NORMAL opening acceptance remains `UNPROVEN`.

## Follow-up — conflicting recovery member isolation — 2026-10-08

### Finding and repair

Audit reproduced a cohort-wide rejection when one returned member changed an
already-observed source field or anchor, even when sibling members contained
valid requested fills. Validation now quarantines only the conflicting symbol,
restores its exact primary row, and applies valid sibling fills. Invalid
embedded identity and out-of-plan new fields/symbols remain hard errors. If
all reported fills are quarantined, Core records `recovery_state=ERROR`, keeps
missing anchors missing, and leaves recovery required. Idempotent retry retains
the quarantine diagnostics.

### Verification and limits

- TDD reproduced the original failure on a single-symbol conflict with a
  valid sibling fill; regression now proves sibling application and primary
  value retention.
- Additional tests cover an all-conflicting response, anchor and source-field
  conflicts, direct recovery, idempotent retry, and preserved identity/scope
  rejection.
- Recovery/timeline tests: 55 passed; full suite: 924 passed; compileall and
  diff-check pass.
- Inputs are synthetic contract responses paired with hash-pinned real
  Q2Frame primary rows; this does not verify actual Wencai response shape or
  live provider behavior. No Redis/TD/Rabbit connection or production effect.
- TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening remains `UNPROVEN`.

## Follow-up — isolate embedded symbol identity mismatch — 2026-10-08

Audit found that one recovery row whose embedded `symbol` disagreed with its
mapping key still aborted otherwise valid sibling fills. Plan-bound and direct
recovery now quarantine that member, never reassign its values to the embedded
symbol, restore its exact saved primary row when one exists, and continue valid
sibling fills. If no usable fill survives, recovery is recorded as `ERROR`
and remains required. Plan/date/revision envelope mismatches stay hard errors.

Red/green regressions cover both recovery entry points, a mismatched member
beside a valid sibling, retention of the primary value, idempotent plan-bound
retry, and a mismatch-only response. The recovery rows are contract inputs
paired with a pinned real Q2Frame primary baseline; they are not live Wencai
evidence. Full Core suite: 927 passed; compileall and diff-check pass. No live
data source, production service, or write path was used.

## Follow-up — quarantine malformed recovery member declarations — 2026-10-08

### Finding and repair

`RecoveryResultV1` normalized declared fill and invalid-symbol tokens as a
whole-set operation. One malformed token such as `BAD` therefore raised before
valid sibling fills could be used. Member tokens are now normalized
individually; invalid ones are excluded and represented by stable anomaly
codes. A diagnosed response with no surviving fill can reach the timeline,
which records an `ERROR` revision, leaves primary facts `PARTIAL`, and keeps
recovery required. An otherwise clean empty `APPLIED` response remains a hard
contract error.

### Verification and limits

- RED reproduced the exception for both malformed `filled_symbols` and
  malformed `invalid_symbols`; GREEN confirms valid siblings survive.
- A bad-only response based on the pinned real 2026-09-30 Q2Frame primary
  fixture records `ERROR`/`PARTIAL` and remains retryable. The recovery
  response itself is contract input, not captured Wencai output.
- Recovery/timeline/opening tests: 71 passed; full Core suite: 931 passed;
  compileall and `git diff --check` pass.
- No live Wencai/Redis/TD/Rabbit access, service action, or production write
  occurred. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening remains
  `UNPROVEN`.

## Integration boundary — static engine-next source audit — 2026-10-08

The inspected active `engine-next` release is
`/home/exedev/services/engine-next/releases/20260903_e272842`. Its
`IntradayDataHub` defines an optional `wencai_auction_fetcher`, but the
constructor defaults it to `None`; no injection call site was found in the
searched active-release tree. The static mapping retains symbol,
`change_pct`, and amount fields, but not the Core 09:25 anchor price or
historical `available_at`. Thus this inspection does not establish a usable
or active Wencai recovery path for the Core 09:25 anchor. It is a source-only
finding: no Wencai request, Redis/TD/Rabbit operation, or production write was
performed, and no engine-next source was changed. A production recovery
integration remains separate work and must be verified against its actual
owner and payload before claiming availability.

## Follow-up — isolate malformed expected auction-universe members — 2026-10-08

### Finding and repair

`build_auction_anchor_revision()` previously normalized the entire expected
universe in one set comprehension. A single malformed symbol could therefore
throw away valid observed anchor rows. Expected symbols are now normalized
individually; malformed members are quarantined as `INVALID_EXPECTED_SYMBOL`.
Valid observed anchor facts remain available, while anchor/source coverage is
withheld and the revision stays `PARTIAL` because the declared denominator is
not trustworthy. If all expected members are invalid, observed facts remain
visible but the universe and coverage stay unknown, and recovery remains
available.

### Verification and limits

- RED reproduced `ValueError` using the pinned real 2026-09-30 Q2Frame primary
  row and a malformed expected member; GREEN verifies the fact is retained,
  coverage is not asserted, and the revision is `PARTIAL`.
- Both valid-plus-invalid and all-invalid universe cases pass. Full Core suite:
  933 passed; compileall and `git diff --check` pass.
- The source row is a hash-pinned fixture, not a live read in this run. No
  Redis/TD/Rabbit access, production service action, or production write
  occurred. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening remains
  `UNPROVEN`.
