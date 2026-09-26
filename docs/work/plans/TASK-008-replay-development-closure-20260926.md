# TASK-008 replay-for-development execution plan

Date: 2026-09-26 (Asia/Shanghai)
Owner: `replay-investigator` (sequential `tester` and `auditor` reviews)
Status: `IN_PROGRESS`; Phases 3–4 have bounded real-data evidence recorded; no
successor business phase has been started.

## 1. Goal and current facts

The goal is a replay that can safely support development of the live t1-v2/Core
calculation path using real market input. It is not a claim that historical TD
can reconstruct Rabbit delivery order, wall-clock visibility, or a NORMAL
opening.

At plan time:

- t1-v2 is clean at `16beee67cf778cebaa20fb442ca178d01b301979` on
  `codex/task-q2-pure-function`; `ca5ece0` is an ancestor of this HEAD.
- Core is clean at `d67615576f4baaa122a1418111702c7c276b3b98` on
  `codex/feature-session-engine-integration`, 34 commits ahead of its tracked
  remote branch.
- Existing real evidence includes a 2026-09-23 full-window TD replay using
  500 three-second SELECT slices, 1,204,178 ticks, t1-v2 Q2 output for 5,222
  symbols, and repeatable isolated Redis outputs. The post-open comparison is
  repeatability evidence; the retained DB5 baseline ends at 09:25:09.
- The 2026-09-23 pre-open source-aligned replay and barrier trace matched the
  retained Q2 and 0920/0924/0925 auction projections for that bounded date and
  window. This does not establish all-date or Rabbit-arrival parity.
- `TASK-008=PARTIAL_EVIDENCE`, `REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
  `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
  `TD_WRITE_HEALTH=UNPROVEN` remain the recorded states.

### Auction event time versus arrival/availability time (non-gating context)

- Operator-provided market fact: auction tick event timestamps do not exceed
  09:25, while queue delivery/processing may continue after 09:25. This is
  context for interpreting snapshots, not a new gate or a reason to stop the
  existing execution plan.
- In the checked wire contract, `DataRecord.tss` is per-tick, while
  `DataBatch.sent_at` and outer-header `timestamp` are separate metadata. The
  consumer maps `tss` to `RawTick.ts_ms` and the outer timestamp to
  `TickBatch.wall_ts_ms`; this consumer code does not prove how the publisher
  assigns those fields or record per-tick receive time. Publisher provenance
  remains `UNKNOWN` in the current evidence.
- Keep the current replay phases and acceptance flow unchanged. Interpret TD
  replay as testing calculations over real event-time rows; do not present its
  event-time ordering as proof of Rabbit arrival timing or exact live snapshot
  membership. This is a known evidence limit, not an additional pass/fail
  condition.

The execution must first check whether newer evidence already closes a step;
do not repeat a successful run just to regenerate the same report.
The recorded full-window 500-slice replay predates later slice/batch and
same-symbol preservation commits in the current t1-v2 line; Phase 0 must
confirm the exact binary manifest. Unless it proves those changes were included,
that run is not current-HEAD acceptance and Phase 2 is required once.

## 2. Non-negotiable boundaries

- TD access is `SELECT` only. No TD insert, retention change, or cleanup.
- Do not consume Rabbit, change ACK behavior, or claim Rabbit arrival order.
- Do not write production Redis DB0. Validation Redis writes are permitted only
  with an explicitly selected nonzero DB, a fresh unique namespace per run,
  and verified zero namespace hits in DB0. Do not delete old evidence/keys.
- Do not modify or restart deployed services/producers or production data.
  Local development-branch t1-v2/Core edits are allowed only after a concrete
  Phase 0–4 discrepancy is reproduced and scoped to an authorized file.
- Do not run a replay from a historical validation snapshot instead of the
  current development worktree.
- Do not print credentials, tokens, cookies, or auth-file content.
- Do not bulk-load the full time range, use arbitrary row chunks, or gzip a
  substitute data path. Query one `[start,end)` three-second time slice at a
  time; at most one completed slice plus one explicitly bounded prefetch may
  reside in memory. Empty slices remain in the timeline.
- Preserve every returned source row in the baseline path. Same-symbol
  de-duplication is an experiment, never an unverified default.
- Elapsed time alone is not a failure gate. Record progress and resource use;
  stop only for a concrete safety/resource risk or a diagnosed non-progressing
  operation, then preserve evidence and continue with a narrower experiment.
- No M3-1, strategy migration, deployment, or automatic next-task promotion.

## 3. Fixed replay semantics to verify

1. The TD frame is `[frame_start_ms, frame_end_ms)`; the right edge belongs to
   the next frame. Frame sequence advances even for an empty frame.
2. A frame is the time-slice envelope, not an assertion that all symbols have
   one identical source timestamp. Retain each tick's own event time.
3. Feed t1-v2 through its shared `SourceTickBatchBuilder` and `TickBatch`
   processing path. Do not create a second Q2 calculation in Core.
4. Run all rows from each slice in the baseline. Deterministic replay ordering
   is not evidence of historical Rabbit order. Same-time/same-symbol rows with
   no source sequence remain order-ambiguous.
5. Business-time cutoff comparisons discard subsecond precision without
   overwriting the raw source timestamp: `09:25:06.000`, `.197`, and `.999`
   belong to business second `09:25:06`; `09:25:07.000` is the next second.
   Verify whether each same-second tick is before or after the snapshot by the
   actual shared batch/barrier processing order and real captured output. Never
   infer Rabbit arrival from TD event time. A later business second must not be
   included just because the row was returned by a wider query.
6. Freeze Q2/auction outputs at each tested cutoff into immutable validation
   artifacts or isolated per-cutoff namespaces. Never read the final Redis
   state after 09:40 and call it the historical 09:32 or 09:25 state.

## 4. Phased execution

### Phase 0 — Reconcile existing evidence (read-only; first action)

Read the latest task board, current-task note, source-alignment handoff,
barrier-trace handoff, and validation manifests. Confirm the exact commits and
binary hashes used by each result. Make a table of `proven`, `bounded`,
`unknown`, and `not tested` claims. Do not edit task states yet.

**Pass:** no contradictory evidence is silently combined; the selected next
test adds a specific missing fact.
**If stale/conflicting:** compare manifest hashes and run boundaries; preserve
both results, identify which source/build/date each covers, and only then pick
the experiment that distinguishes them.

### Phase 1 — Verify the shared input and calculation path (read-only)

Trace the production decoder and replay source through these current files:

- `C/t1_v2/rabbitmq_batch_decoder.cpp`
- `C/t1_v2/source_tick_batch_builder.cpp`
- `C/t1_v2/tick_batch.h`
- `C/t1_v2/td_replay_query.cpp`
- `C/t1_v2/td_replay_row_converter.cpp`
- `C/t1_v2/td_replay_tick_source.cpp`
- `C/t1_v2/engine_core.cpp`
- `C/t1_v2/runtime_pipeline.cpp`
- `C/t1_v2/redis_v2_writer.cpp`

Record a field-and-boundary matrix: Rabbit decoded fields, TD row fields,
normalization/default handling, source timestamp, producer sequence/arrival
metadata, batch construction, per-tick processing order, Q2 calculation, and
Redis serialization. The code already routes both sources through
`SourceTickBatchBuilder`; verify this remains true at the current HEAD. The
actual Rabbit delivery grouping and arrival order remain `UNKNOWN` unless a
separately authorized capture proves them.

**Pass:** replay and Rabbit use the same `TickBatch` type, builder, and Q2
calculation path; only the source adapter, replay control events, and sink
policy differ.
**If not:** identify the smallest divergent function and add a real-row or
golden regression before changing code. Do not redesign the DTO or decoder by
speculation.

### Phase 2 — One current-HEAD full-window real replay

Use real TD `market_data1.stock_tick_v2` for 2026-09-23
`[09:15:00,09:40:00)`, one SELECT per three-second frame. This date is chosen
because both pre-open retained comparison evidence and full-window repeat
evidence exist. Read all returned rows without symbol/time filtering. If the
source is no longer available, mark it `UNAVAILABLE`; do not synthesize rows.

Run the current t1-v2 HEAD in Replay mode into a fresh isolated Redis
validation namespace (nonzero DB, unique run tag), with TD writes explicitly
disabled. Before running, inspect that binary's `--help` and effective replay
configuration; pass the start/end/table explicitly, set `REDIS_DB` to the
approved nonzero validation DB, set unique prefixes for every Redis output
family, and explicitly enable only the validation Redis sink. If any output
key is not covered by the chosen prefix configuration, stop before execution
and use a dedicated isolated database with verified pre/post namespace
inventory. Capture per frame: bounds, row count, distinct symbols, min/max
source timestamp, empty/nonempty status, input digest, accepted/rejected row
counters, and emitted batch count. Include one memory/heartbeat record per
frame; never materialize the whole day.

At 09:20, 09:24, 09:25:06, 09:26, 09:32, and 09:40, save the actual t1-v2
projection available at that replay event into immutable evidence. Use an
existing Q2Frame/snapshot path if it captures the requested cutoff; otherwise
first prove the limitation, then add only a replay-only snapshot seam. Do not
use post-run `latest` keys as earlier snapshots. For whole-second cutoff
behavior, inspect real source rows around `.000/.197/.999` and the next second;
if a particular boundary has no real row, report it as not observed and use a
unit test only for implementation behavior, not as a real-market pass.

**Pass:** all 500 time slices are accounted for, including empty ones; the sum
of per-slice rows reconciles to the replay source count; there are no silent
row drops; rejected rows are enumerated; output cutoff artifacts have
manifests and hashes; `td_sql=0`, `ack=0`; production Redis DB0 has no run-tag
keys. Do not require every symbol to update in every frame or require zero
stale/missing facts.
**If mismatch/error:** retain stdout/stderr, exit status, last completed frame,
and counters. Classify connection/query/schema/conversion/Redis/output issues
before retrying. Use a new namespace for every retry so partial writes cannot
masquerade as a clean run.

### Phase 3 — Real-data grouping and de-duplication experiment

Use only rows from the Phase 2 manifest. Compare, as validation-only variants:

- A: preserve all rows in each three-second slice (baseline/current behavior).
- B: retain only the latest event-time row per symbol in each slice; resolve
  same-time ties only with a stable source row key, otherwise label the result
  order-ambiguous.

Do not silently ship B. Compare row accounting and per-cutoff Q2 hashes,
active-symbol sets, 0920/0924/0925 auction outputs, 0926/latest summaries, and
Core facts. Also identify whether dropped intermediate rows affect fields
derived from event deltas or only cumulative snapshots.

**Decision:** if B changes any required output, keep A for that behavior and
document which downstream feature requires intermediate ticks. If B is equal
for this date/window, record only `PASS_WITH_LIMITS` for the tested source and
feature set; do not claim universal equivalence. A default filtering change
requires a separate narrow implementation/test and must never hide the
discarded-row count.

### Phase 4 — Repeatability and Core consumption of producer Q2

Repeat the same frozen input/build in a second fresh isolated namespace. Compare
canonical semantic outputs, excluding run-specific wall-clock telemetry,
Redis byte counters, and namespace-dependent fields. Then feed the captured
producer Q2 snapshots—not TD-derived Core guesses—through the Core Q2Frame or
Redis read adapter for each supported cutoff. Run ordered/shuffled Core
comparisons over the same frozen cohort where the adapter supports it.

**Pass:** repeated Q2/auction semantic hashes match for equal input/build;
Core consumes the exact producer output; `MISSING`, `STALE`, `PARTIAL`, and
`UNAVAILABLE` remain visible; strategy conclusions are not emitted as facts.
Coverage is measured against the actual source/cohort manifest, not a guessed
universe.
**If different:** first verify input-frame digests and source/binary hashes;
then compare the earliest divergent frame and smallest output surface. Do not
patch from only a final aggregate hash.

### Phase 5 — Sequential test and audit, then return to mainline

1. `tester`: run full-dependency t1-v2 build/self-test, focused real-data
   repeatability/comparison checks, Core targeted tests, then the full Core
   pytest/compileall/diff-check suite.
2. `auditor`: independently check source boundary, tick accounting, three-
   second half-open cuts, cutoff snapshots, de-duplication decision, Redis DB
   isolation, TD write count, ACK count, hashes, and every remaining unknown.
3. Integrator: reconcile findings against the original requirement. Commit only
   a minimal code fix with its tests and report; documentation-only changes do
   not count as replay completion. Do not push, merge, deploy, or change task
   status unless separately requested.

Every phase ends with a short alignment review: requirement → evidence → gap →
next smallest discriminating test. Continue after a technical failure by
collecting facts and testing a concrete hypothesis; do not repeat an unchanged
failed command. Ask the user only when the remaining issue requires a business
semantic choice (for example, whether an observed, output-changing de-duplication
is acceptable).

## 5. Evidence bundle

Write reports under a fresh
`/home/exedev/validation/task008-replay-development-<timestamp>/` directory:

- `execution_manifest.json` — repo commits, binary hashes, date/window,
  commands with secret values redacted, Redis DB/tag, TD write disabled;
- `source_contract_matrix.md` — Rabbit/TD shared-type and field map plus
  unknowns;
- `frame_inventory.csv` — one row per three-second slice, including empty
  slices and digests;
- `cutoff_projection_hashes.json` — immutable Q2/Auction/Core results per
  cutoff and comparison status;
- `dedup_experiment.json` — all-rows vs latest-per-symbol real-data result;
- `repeatability.json` — semantic hash comparison and excluded telemetry;
- `side_effect_audit.json` — `td_sql`, `ack`, Redis DB/prefix checks, services;
- `unknowns_and_limits.md` and `sha256sums.txt`.

Do not include credentials or dump an unbounded raw market dataset into the
report. Preserve existing historical evidence and validation directories.

## 6. Completion vocabulary and non-gates

The useful outcome for this task is feature-scoped:

```text
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
SHARED_T1V2_TICKBATCH_PATH=PASS or documented gap
FULL_WINDOW_REAL_TD_PROCESSING=PASS or PARTIAL with exact missing slices
PRODUCER_Q2_TO_CORE_CONSUMPTION=PASS or NOT_COMPARABLE by cutoff
DETERMINISTIC_REPEAT=PASS or NON_DETERMINISTIC with first divergent frame
LIVE_RABBIT_ORDER_EQUIVALENCE=UNKNOWN until captured
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

Historical `available_at`, exact Rabbit arrival order, universal de-duplication
equivalence, zero stale symbols, and an arbitrary runtime ceiling are not
general gates for replay-led feature development. They remain limitations or
feature-specific evidence needs. A feature may proceed only when the fields it
uses have the required source/version/unit/time evidence.

## 7. Current next action

Phases 0–1 were reconciled against the current t1-v2/Core branches and source.
The exact 2026-09-18 full-window replay and same-symbol experiment are recorded
in `docs/work/handoffs/TASK-008-PHASE3-DEDUP-AB-20260926.md` and
`/home/exedev/validation/task008-dedup-20260926T210044+0800/dedup_ab_report.md`.

The experiment rejects latest-per-symbol de-duplication as a transparent
pre-processing step: it changes 09:20 facts and Q2 state. The default must
continue to preserve all rows through the shared t1-v2 pipeline. Do not repeat
the same de-dup experiment, promote a successor task, or treat this result as
NORMAL acceptance.

Remaining bounded evidence gap: the current t1-v2 runner logs aggregate counts,
not a per-frame row-count/digest inventory. Existing real SELECT inventory
records 500 frames/98 empty frames, but current direct replays did not emit a
per-frame manifest; report this limit without turning it into a general
development stop. Continue only with the next feature-specific question from
the original replay-for-development goal.

## 8. Execution alignment review — 2026-09-26

| Plan phase | Result | Evidence/limit |
|---|---|---|
| Phase 0: reconcile | Complete, read-only | Current source branches and prior validation manifests reconciled; stale DB7 attempt excluded because its terminal summary/exit code were not preserved. |
| Phase 1: shared path | Complete, source-audited | Rabbit and TD both use `SourceTickBatchBuilder`/`TickBatch` and the shared runtime pipeline; Rabbit delivery grouping remains unknown. |
| Phase 2: full-window replay | Bounded pass | A and B each issued 500 sequential three-second SELECT slices for 09/18 and reported 1,224,811 source rows; `source_reject=0`, `ack=0`, `td_sql=0`. Current per-frame input digests/counts were not emitted. |
| Phase 3: de-dup experiment | Experiment complete; latest-only equivalence rejected for observed outputs | B removed 2,728 rows, with 0 ambiguous max-time groups; changed Q2 state and 09:20 auction facts. Keep A as default. A/B had equal aggregate input counts but no per-frame payload digest, so exact row-for-row causal attribution is unproven. This is not a universal result for other dates/features. |
| Phase 4: repeatability/Core consumption | Bounded pass with limits | Same-build all-row replays to DB10/DB11 matched semantically except M2 `redis_bytes`; per-frame input identity was not recorded. Core read-only adapters consumed A/B Q2 and auction data; both A/B barrier Q2Frame replays were deterministic. Core Q2 projection hash equality excludes raw-only fields and does not erase the raw Q2 differences. |
| Phase 5: tests/audit | Local tester and self-audit complete | Full-dependency t1-v2 build/self-test passed; Core `705 passed`, compileall and diff-check passed. A separate independent second-agent audit was not run. No push, merge, deploy, service restart, or M3-1 execution. |

Current states remain:

```text
TASK_008=PARTIAL_EVIDENCE
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```
