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
  `DataBatch.sent_at` and the body-embedded outer wire-header `timestamp` are
  separate metadata. The consumer maps `tss` to `RawTick.ts_ms` and that inner
  wire timestamp to `TickBatch.wall_ts_ms`; it does not read AMQP
  `BasicProperties.timestamp` or record per-tick receive time. This consumer
  code does not prove how the current publisher assigns the inner fields.
  Publisher provenance remains `UNKNOWN` in the current evidence.
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

The 09:25:06 event-time barrier already has bounded real-data evidence in
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_TRACE_AUDIT_20260925.md`: on
2026-09-23, all traced source timestamps were at or before the barrier, the
5,208-symbol anchor member set matched the archived Redis anchor, and the
isolated Q2/auction outputs matched the retained DB5 baseline. Do not repeat a
generic barrier-algorithm replay. Live wall-clock visibility remains unknown.

Source inspection confirms the wire fields `DataRecord.tss` and
`DataBatch.sent_at`; the current decoder copies `tss` into the tick source time
and reads the body-embedded length-prefixed JSON wire-header timestamp as wall
metadata, not the AMQP `BasicProperties.timestamp`. A search of the
available repositories found no production publisher assignment proving
whether those values are exchange event time, publish time, or another clock.
Keep that timestamp-origin question open, while allowing development of
features that do not depend on the missing arrival-time evidence.

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

## 9. Execution alignment review — 2026-09-27 09:25 cohort

- **Requirement:** establish from real data whether the saved 09:25 auction
  state resembles one uniform event-time replay frame, without claiming that
  TD order reveals Rabbit arrival or adding a new completeness gate.
- **Evidence:** read-only query of real
  `market_data1.auction_snapshot_v2` returned 5,221 rows for trade date
  `20260918`, tag `0925`. A streamed comparison against the real-TD-derived
  t1-v2 Q2Frame artifact matched each snapshot's
  `(match_amt_yuan, rest_bid_amt_yuan, rest_ask_amt_yuan)` tuple to one or more
  of replay seq 200/201/202 for 5,221 symbols. Exact tuple match counts were
  813/5,221, 4,537/5,221, and 3,588/5,221 respectively; patterns span multiple
  frame states. The post-reboot rerun reproduced these counts and artifact
  SHA-256. Full method, selected rows, host/repository recheck, caveats, and
  checksums are in
  `/home/exedev/validation/task008-0925-snapshot-cohort-audit-20260927/`.
- **Result:** observed mixed per-symbol state at the saved snapshot, consistent
  with staggered updates. An atomic completed TD frame is not evidence that
  all corresponding live updates were visible at one wall-clock instant.
  This comparison uses only three numeric fields and is not a unique event
  identity.
- **Unknown:** historical Rabbit batch membership, per-tick arrival/availability,
  publisher/build identity for the frozen snapshot, and whether the differing
  states were caused by delivery delay rather than another producer or
  projection behavior. No raw Rabbit capture was found in this evidence path.
- **Next smallest useful step:** for a feature that needs the frozen 09:25
  state, use the actual captured TD snapshot as a point-in-time oracle/reference
  and compare only the fields the feature uses. Do not replace the replay input
  with that snapshot unless the corresponding live feature actually reads the
  frozen Redis key. Keep event-time replay for calculations over tick rows. Do
  not emulate Rabbit batches or claim arrival parity unless an existing raw
  capture/build record provides that evidence.
- **Alignment/status:** no code, task-board, or production change; no new hard
  gate. `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`, and
  `TD_WRITE_HEALTH=UNPROVEN` remain unchanged. Production side effects:
  `NONE_OBSERVED`.

## 10. Post-reboot Redis Q2 audit — 2026-09-28

- **Observed:** after the 2026-09-27 host reboot, the running `t1-v2-live`
  release reported one recovered batch at 20:15:30 with 1,000 source records,
  200 rejects, 800 accepted ticks, one ACK, `redis_committed=800`, and
  `td_sql=0`. Its source time was 2026-09-24 15:29:39 +08:00. A read-only
  Redis inspection found `q2:active:20260924` with 800 symbols; their Q2
  source times had 416 distinct values spanning 15:00:00–15:29:39. At the
  2026-09-28 01:17 check, every hash TTL was 68,031 seconds; active sets for
  09-27/28 were empty.
- **Exact-release source finding:** the deployed process starts an empty
  in-memory quote store; older ticks are not discarded by `apply_base_tick`,
  and the Q2 writer has no comparison with the existing Redis `ts` before
  issuing `HSET`. This establishes a stale-overwrite mechanism, not proof that
  newer pre-existing values were actually overwritten: no pre-write Redis
  snapshot or raw batch identity is available. Current zero auction fields
  are consistent with fresh-state reset, but do not prove that prior nonzero
  fields were erased.
- **Boundary:** the running release's local patch suppresses TD writes after
  09:45 while leaving Redis and ACK paths active. Therefore the observed
  `td_sql=0` for 15:29 input is expected under that release contract; Redis
  Q2 writes and ACK were observed. The audit itself was read-only.
- **Next step:** do not add a hard production gate from this finding. Seek a
  raw/captured batch identity and pre-write Redis snapshot if available; absent
  those, validate the mechanism only in an isolated Redis environment with
  real captured inputs and label the historical causal question unresolved.
  Full evidence and code-path details:
  `docs/work/handoffs/TASK-008-POST-REBOOT-Q2-AUDIT-20260928.md`.
- **Status:** `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`, and
  `TD_WRITE_HEALTH=UNPROVEN` remain unchanged. No code, task-board, service,
  or production data was changed by this audit.

## 11. Same-day event-time ordering mechanism check — 2026-09-28

- **Question:** determine whether processing an older event-time slice after a
  newer same-day slice can move the generated Q2 timestamp backward. This is a
  mechanism check only; TD event-time order must not be treated as Rabbit
  arrival order.
- **Method/evidence:** using the current t1-v2 development build and actual
  rows read from `market_data1.stock_tick_v2`, process the 2026-09-24
  `[09:30:03,09:30:06)` slice first, then `[09:30:00,09:30:03)`. Each run
  queried one 3-second half-open slice and one batch. Writes were confined to
  a unique Redis DB15 validation namespace with a 600-second TTL; DB0 had no
  matching namespace keys before or after. No Rabbit consume/ACK or TD write
  occurred. Evidence, command manifest, limits, and checksums are in
  `/home/exedev/validation/task008-same-day-order-20260928T063420+0800/`.
- **Observed:** the later slice processed 5,101 rows, then the earlier slice
  processed 4,580 rows. Among 5,064 Q2 keys present after the later slice and
  still comparable after the earlier slice, 4,455 timestamps regressed
  (87.98%), 609 were unchanged, and none advanced. For `000001`, Q2 changed
  from event time 09:30:03 (`px=11360`) to 09:30:00 (`px=11370`). The
  same-day date guard skipped zero writes. This confirms the isolated
  real-input mechanism: the current writer permits same-day timestamp
  regression when input is deliberately processed in reverse event-time
  order.
- **Interpretation/limits:** the slice order was deliberately reversed; the
  result does not show that production Rabbit delivered these batches in that
  order, nor that a production Q2 value was historically overwritten. Live
  same-day arrival order and historical pre-write Redis state remain
  `UNKNOWN`/`UNPROVEN`. In particular, do not add an event-time rejection
  gate based on this result alone: delayed auction ticks may carry event times
  at or before 09:25 while arriving later.
- **Alignment/next step:** no production code, service, task-board state, or
  task structure changed, and no new hard gate was introduced. If an existing
  raw batch/arrival capture and corresponding pre-write Q2 evidence become
  available, compare them; otherwise retain this as a mechanism result and
  continue feature-scoped replay work that does not require Rabbit-arrival
  reconstruction. Do not stop TASK-008 or promote a new phase on this evidence
  alone. Status remains `TASK-008=PARTIAL_EVIDENCE`,
  `M3_1_NORMAL=BLOCKED`, `TD_WRITE_HEALTH=UNPROVEN`,
  `PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED`.

## 12. Chronological control for same-day Q2 order sensitivity — 2026-09-28

- **Requirement:** pair the previous deliberately reversed real-TD slice
  experiment with the natural event-time order, and distinguish an invocation
  configuration failure from an actual source/data failure.
- **Initial failure and diagnosis:** the first attempt used the binary default
  `TDENGINE_HOST=chaos`, which did not resolve in the current shell. It exited
  at source startup with zero batches, Redis commands/commits, and TD
  statements. Read-only inspection found the TD container publishing
  `127.0.0.1:6030`; the same exact binary connected and replayed when that
  endpoint was explicitly set.
- **Evidence:** actual `market_data1.stock_tick_v2` rows for adjacent
  `[09:30:00,09:30:03)` and `[09:30:03,09:30:06)` slices were processed in
  chronological order through the current t1-v2 development binary. The first
  slice produced 4,580 Q2 symbols; the second processed 5,101 rows and left
  5,189 symbols. Among 4,580 common symbols, 4,455 Q2 timestamps advanced,
  125 stayed equal, and none regressed. Full projection digests, command/run
  counters, and environment-failure diagnosis:
  `/home/exedev/validation/task008-same-day-order-control-20260928T064800+0800/`.
- **Interpretation:** paired with the earlier later-first test, this confirms
  that the reproduced same-day Q2 rollback is order-sensitive under current
  t1-v2 behavior. The chronological control is not per-symbol inverse proof
  because its common-symbol set differs from the reversed experiment. Neither
  result proves actual production Rabbit order or a historical production
  overwrite. No strict timestamp-rejection gate follows; late auction facts
  remain possible and must not be silently dropped.
- **Alignment/status:** no source code, task-board, service, or production
  data changed; Redis writes were confined to unique DB15 validation
  namespaces with 600-second TTL, and TD writes/Rabbit ACK remained zero.
  Services stayed active with `NRestarts=0`. No task was promoted and no hard
  gate was added. Status remains `TASK-008=PARTIAL_EVIDENCE`,
  `M3_1_NORMAL=BLOCKED`, `TD_WRITE_HEALTH=UNPROVEN`,
  `PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED`.

## 13. Full-day cross-day Q2 date-guard verification — 2026-09-28

- **Question:** determine whether an older trade-date tick is skipped after a
  full current-date Q2 universe has been established, rather than after only
  one narrow three-second frame.
- **Method/evidence:** using the exact t1-v2 development commit `b2aa169` and
  binary SHA-256 recorded in the evidence manifest, replay actual
  `market_data1.stock_tick_v2` rows for 2026-09-24 `[09:15:00,09:40:00)` as
  500 sequential three-second slices into an isolated Redis DB15 namespace;
  then feed the actual 2026-09-23 `[09:30:00,09:30:03)` slice into that same
  namespace. TD writes were disabled. The namespace was absent before the
  run, DB0 remained untouched, and the full evidence plus checksums are in
  `/home/exedev/validation/task008-crossday-full-baseline-20260928T070100+0800/`.
- **Observed:** the 500-frame baseline processed 1,194,572 source rows and
  produced 5,222 Q2 hashes, all dated 2026-09-24. The older slice contained
  4,342 rows; all 4,342 Q2 writes were skipped by the date guard. Hashes of
  all fields across the 5,222 Q2 hashes and the 2026-09-24 active-set
  membership were unchanged after the older slice. Both invocations reported
  `td_sql=0` and `ack=0`; the baseline reported `clocks=0`.
- **Reconciliation:** the earlier 458 writes occurred after a single
  three-second baseline and were symbols absent from that narrow frame. They
  do not demonstrate a full-day cross-date guard leak. With the full-day
  universe present, this observed previous-day slice caused no older-date Q2
  mutation.
- **Limits/alignment:** this is development-build evidence for one real
  current-day full replay followed by one prior-day slice. It does not prove
  production Rabbit order, the post-reboot batch identity, live-binary
  behavior, causal production overwrite, auction barrier behavior, or handling
  of every symbol/date combination. In particular, `clocks=0` means this was
  not an auction snapshot acceptance test. No same-day event-time rejection
  rule follows; late-arriving auction ticks remain possible. No source code,
  production service, or production data was changed; Redis writes were
  isolated in DB15 under a 3,600-second TTL namespace.
- **Status:** `FULL_DAY_TD_SLICED_REPLAY=PASS`,
  `CROSS_DAY_DATE_GUARD_ON_OBSERVED_SLICE=PASS`,
  `PRODUCTION_RABBIT_ARRIVAL_EQUIVALENCE=UNKNOWN`,
  `PRODUCTION_CAUSAL_OVERWRITE=UNPROVEN`,
  `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`,
  `TD_WRITE_HEALTH=UNPROVEN`, and
  `PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED`.

## 14. Active release binary vs development Q2 date guard — 2026-09-28

- **Question:** did the cross-day Q2 date guard verified on the development
  build reach the currently running `t1-v2-live` binary?
- **Evidence:** read-only systemd/process inspection found MainPID `318`,
  `NRestarts=0`, and `ExecStart` through the `current` symlink to release
  `20260923_tdstop0945b`. The active `/proc/318/exe`, symlink target, and
  archived release binary have the same SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.
  Release `build_info.json` identifies its local delta as a TD-after-09:45
  storage patch; the deployed `redis_v2_writer.cpp` hash matches the release
  source manifest. Its Q2 path uses ordinary `HSET` and contains no
  `Q2DateAwareHSet`/cross-day stored-`ts` comparison. The initial t1-v2 log
  check found no newer entry than the 2026-09-27 20:15:30 recovered-batch
  progress line (`source_in=1000`, `source_reject=200`, `ticks=800`, `ack=1`,
  `td_sql=0`, `redis_committed=800`); a later journal query found an 08:41:04
  progress line, recorded in section 17. The initial check's absence was not
  evidence of queue idleness or no unlogged activity. Full report:
  `/home/exedev/validation/task008-live-q2-date-guard-audit-20260928T074236+0800/`.
- **Conclusion:** the current running release does not contain the date guard
  verified in development commit `b2aa169`. This confirms a code-path gap;
  it does not prove the post-reboot recovered batch caused an overwrite,
  because the pre-write Q2 values and raw Rabbit batch identity are absent.
- **Alignment:** the finding does not justify a same-day timestamp-order gate
  or dropping delayed auction ticks. No service, Redis, TD, Rabbit, or
  production file was changed. No restart or deployment is authorized by this
  audit. A release promotion remains a separate decision; meanwhile continue
  feature work against the pinned real replay evidence and keep live
  equivalence claims `UNPROVEN`.
- **Status:** `ACTIVE_BINARY_MATCHES_RELEASE_ARTIFACT=YES`,
  `RELEASE_SOURCE_HAS_Q2_CROSS_DAY_GUARD=NO`,
  `POST_REBOOT_CAUSAL_OVERWRITE=UNPROVEN`,
  `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`,
  `TD_WRITE_HEALTH=UNPROVEN`.

## 15. Exact active-release functional cross-day Q2 replay — 2026-09-28

- **Question:** verify whether the exact executable used by `t1-v2-live` can
  functionally overwrite a newer-date Q2 projection with an older-date one,
  beyond the earlier source inspection.
- **Method:** the active-release executable (SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`)
  queried real `market_data1.stock_tick_v2` rows for 2026-09-24 and then
  2026-09-23, one `[09:30:00,09:30:03)` slice per invocation. Redis writes
  were routed to a fresh unique namespace in DB15; TD output was disabled by
  environment and CLI flag. Dry-runs first confirmed both slices were
  readable with zero Redis commands. Exact counters, digest summaries, side
  effect controls and checksums are in
  `/home/exedev/validation/task008-live-q2-crossday-replay-20260928T0757+0800/`.
- **Observed:** 2026-09-24 produced 4,580 Q2 hashes, all with 2026-09-24
  `ts`. The 2026-09-23 slice processed 4,342 ticks and committed 4,342 Q2
  states. The date active sets shared 3,884 symbols; every shared symbol's
  resulting Q2 `ts` was 2026-09-23, with none retaining a 2026-09-24 `ts`.
  Total Q2 hashes after the run were 5,038 (4,342 old-date, 696 new-date).
  The older replay changed `000001` from 2026-09-24 09:30:00 to 2026-09-23
  09:30:00. Both writes reported `td_sql=0`, `ack=0`. DB0 had no matching
  audit-prefix keys; all 5,042 resulting validation keys were in DB15 under
  the explicit namespace.
- **Conclusion/limits:** this functionally reproduces the stale cross-date
  overwrite mechanism in the exact active-release executable's TD-replay
  writer path using real TD rows. It is not a live Rabbit test and does not
  prove the post-reboot Rabbit batch caused the historical production state;
  raw Rabbit identity/arrival order and a pre-write production snapshot are
  still unavailable. It does not justify a same-day event-time rejection
  gate, which could discard delayed auction ticks. This is stronger than the
  prior source-only finding but remains mechanism evidence, not historical
  causal proof.
- **Side effects/alignment:** writes occurred only in isolated Redis DB15;
  those validation keys remain for evidence and were not deleted. TD writes,
  Rabbit consumption/ACK, service restart/deployment, and production-file or
  source changes were not performed. `t1-v2-live` remained PID 318, active,
  `NRestarts=0`; disk remained 45% used with 21G available. No repair, hard
  gate, or task-board transition was made.
- **Status:** `EXACT_RELEASE_BINARY_REPLAY=PASS`,
  `REAL_TD_SLICE_READ=PASS`, `ISOLATED_DB15_Q2_WRITE=PASS`,
  `OLDER_DATE_REPLACED_NEWER_Q2_TS=REPRODUCED_FOR_3884_COMMON_SYMBOLS`,
  `POST_REBOOT_RABBIT_CAUSAL_OVERWRITE=UNPROVEN`,
  `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`,
  `TD_WRITE_HEALTH=UNPROVEN`.

## 16. Candidate date-guard differential and same-day control — 2026-09-28

- **Question:** compare the exact active release with the existing
  development candidate on the same real TD slices, and ensure the candidate
  does not turn cross-date protection into same-day timestamp rejection.
- **Pinned code:** active release SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`;
  development candidate commit `b2aa169c3dddf5d5c7c452f4893dcc01848e358f`,
  binary SHA-256
  `7f7523d3d60ff8dd1bfb7244149634bd1f8bdb71dc88ca8a4bd67057cdf9555e`.
  Both consumed actual `market_data1.stock_tick_v2` 3-second half-open
  slices; candidate self-test passed.
- **Cross-date candidate result:** with the 2026-09-24
  `[09:30:00,09:30:03)` slice first (4,580 ticks), the older 2026-09-23
  equivalent (4,342 ticks) caused 3,884 `redis_q2_date_guard_skips` and 458
  Q2 commits. Final candidate hashes were 4,580 dated 09-24 and 458 dated
  09-23. The 458 were absent from the narrow current-date frame and therefore
  had no existing newer Q2 hash to overwrite. In the separate full-day
  candidate baseline, all 4,342 older-date ticks were skipped after 5,222
  current-date hashes existed. Paired exact-release/candidate evidence:
  `/home/exedev/validation/task008-q2-date-guard-differential-20260928T0815+0800/`.
- **Same-day candidate result:** with actual 2026-09-24 `[09:30:03,09:30:06)`
  processed before `[09:30:00,09:30:03)`, guard skips were zero. Of 5,064
  symbols present in both snapshots, 4,455 Q2 timestamps regressed, 609 were
  unchanged, none advanced. This is deliberately reversed TD replay order,
  not Rabbit arrival evidence. It confirms the candidate guard is
  calendar-date scoped and does not impose same-day event-time monotonicity.
- **Exact-release same-day result:** the active release was run with the same
  real slices, same order and another fresh DB15 namespace. It produced the
  same 5,064 shared symbols, 4,455 regressions, 609 unchanged timestamps, and
  zero advances; `000001` moved from `1790213403000` to `1790213400000`.
  Thus the active binary's same-day regression mechanism is functionally
  reproduced too. The order was chosen by the test, so this still is not
  evidence that Rabbit delivered production batches in that order.
- **Critical relationship to reboot evidence:** the recorded post-reboot
  source time and inspected active-set date were both 2026-09-24. Therefore
  this cross-date guard is not shown to fix the post-reboot observation. The
  missing pre-write per-symbol Q2 values and raw batch identity still prevent
  determining whether the 09-24 batch replaced a newer same-day value. Do not
  claim the cross-date guard closes that incident, and do not add a same-day
  hard rejection from the reverse-order TD experiment.
- **Side effects/alignment:** candidate writes used separate unique DB15
  prefixes; DB0 matches remained zero. `td_sql=0`, `ack=0`; both services
  stayed active without restarts. The DB15 evidence prefixes remain, with no
  delete/flush. No deployment, source edit, task-board transition, or hard
  gate was made. Full-day universe behavior, narrow-frame uncovered symbols,
  and same-day order behavior remain distinct evidence cases.
- **Status:** `DEVELOPMENT_CROSS_DAY_GUARD=PASS_FOR_OBSERVED_CASES`,
  `CANDIDATE_AND_ACTIVE_RELEASE_SAME_DAY_REVERSE_ORDER=REPRODUCED`,
  `POST_REBOOT_SAME_DAY_PREWRITE_COMPARISON=MISSING`,
  `POST_REBOOT_RABBIT_CAUSAL_OVERWRITE=UNPROVEN`,
  `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`,
  `TD_WRITE_HEALTH=UNPROVEN`.

## 17. Post-reboot runtime/logging recheck — 2026-09-28

- **Host/service timeline:** read-only system information at 09:09 +08:00
  reported host boot at 2026-09-27 09:06:54. `t1-v2-live` PID 318 started at
  09:06:57 and remained active with `NRestarts=0`. `engine-next` was active
  (PID 203417), but its journal showed a successful stop/deactivation/start
  at 2026-09-28 00:30:01. The available journal records no reason or actor;
  do not attribute it to the host reboot or this audit. `NRestarts=0` does not
  mean no deliberate service stop/start occurred. Root disk was 45% used,
  21G available.
- **09:09 progress snapshot:** at 08:41:04 the same t1-v2 process reported
  `batches=108`, `source_in=93756`, `source_reject=3608`, `ack=108`,
  `ticks=90148`, `redis_cmds=3208`, `td_sql=4`, `redis_committed=1600`,
  `last_in=1000`, `last_reject=200`, `last_ticks=800`,
  `last_ts_ms=1790524800000`, and `wall_lag_ms=31264098`. Compared with its
  20:15:30 progress line (`batches=1`), 107 additional batches were processed;
  therefore the intervening progress-log gap cannot be interpreted as an
  idle consumer. Release source confirms progress emission is gated by source
  logical-time advancement (`report_interval_seconds`), not wall time.
- **Counter scope:** `batches`, `source_in`, `source_reject`, `ack`, `ticks`,
  `redis_cmds`, `td_sql`, and `redis_committed` are process-cumulative values;
  `last_*` and `last_ts_ms` describe the latest batch. Thus `td_sql=4` reports
  four TD statements in the runtime path since process start, but cannot be
  assigned specifically to the 08:41 batch. It is not a TD readback and does
  not independently prove persisted data or write health.
  `last_ts_ms` converts to 2026-09-28 00:00:00 +08:00; the matching wall lag
  says the tick timestamp was old relative to processing time, not how long it
  sat in Rabbit or where its timestamp originated.
- **Redis sample:** at 09:00:37, `q2:active:20260928` had 5,226 members and
  `q2:600000.ts=1790524800000`; at 09:03, a 24-symbol sample had the same
  timestamp, nonzero price, zero amount/volume, phase 0. This is a bounded
  observation only: cause and whole-set completeness remain unknown.
- **09:18:20 progress snapshot:** same PID's progress advanced to
  `batches=538`, `source_in=224997`, `source_reject=5578`, `ack=538`,
  `ack_fail=0`, `ticks=219419`, `redis_cmds=134668`, `td_sql=434`,
  `redis_committed=66476`, `last_in=162`, `last_reject=0`, `last_ticks=162`,
  `last_ts_ms=1790558300000`, `wall_lag_ms=888`. Consecutive lines were
  present at 09:17:40, :50, 09:18:00, :10, and :20. The source timestamp
  converts to 09:18:20 +08:00. This demonstrates active processing and a
  near-wall-clock source timestamp at that observation, not Rabbit arrival
  latency or producer origin. `td_sql` is cumulative; no TD readback was made.
- **Current check/status:** at 09:18:20 both services were active, with
  `NRestarts=0`, and disk remained 45% used / 21G available. This does not
  erase the engine-next stop/start at 00:30 or establish its cause. No
  production service/data mutation was performed by either read-only status
  check. `TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
  `M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

- **09:23:00 progress update:** a fresh journal read found consecutive
  10-second source-time progress through 09:23:00 on the same PID. Cumulative
  metrics reached `batches=1104`, `source_in=386713`, `source_reject=5612`,
  `ack=1104`, `ack_fail=0`, `reject=9`, `ticks=381101`, `redis_cmds=393140`,
  `td_sql=1002`, and `redis_committed=194527`; latest batch had
  `last_in=196`, `last_reject=0`, `last_ticks=196`,
  `last_ts_ms=1790558580000` (= 09:23:00 +08:00), `wall_lag_ms=318`.
  `td_sql` is a cumulative process counter, not per-batch TD proof; no TD
  readback was made. `source_reject=5612` out of `source_in=386713` (about
  1.45%) counts source records for which `RawTickConverter` returned false;
  the release does not record rejection reasons. The inspected converter can
  reject nonpositive `tss`, malformed six-digit symbols, non-equity
  market/symbol pairs, or an SZ index-price heuristic. Separate `reject=9`
  counts successful Rabbit `basic.reject` calls; `ack_fail=0`, and the last
  batch had `last_reject=0`. The rejected-record causes remain unknown, so this
  is neither a clean-feed PASS nor proof the service is broken. The
  midnight-to-check journal filter returned no
  `No enough disk space`, `stage=commit.tdengine`, or `t1_v2 fatal` matches.
  At 09:23:26, both services were active with `NRestarts=0`, disk use was
  45% / 21G free. This materially strengthens current processing evidence,
  but does not supply the human-confirmed cleanup baseline or establish
  persisted TD health. Status remains `TASK-008=PARTIAL_EVIDENCE`,
  `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
  `TD_WRITE_HEALTH=UNPROVEN`.

## 18. Core full-window continuation — 2026-10-07

The Core runner was extended with an opt-in `--continue-through-input` mode;
the default opening cutoff is unchanged. On the pinned real 2026-09-23 T1
Q2Frame artifacts, ordered and repeat runs each processed all 500 frames /
1,209,672 updates through the last input event at 09:39:59 with one Engine
instance and identical final state hash. The 78 empty frames remained in the
input timeline. Full report and hashes:
`docs/work/handoffs/TASK-008-CORE-FULL-WINDOW-CONTINUATION-20261007.md`.

At the 09:32:10 opening evaluation, business fact, field-status,
cross-section, transition, amount, and limit-summary hashes match the prior
343-frame run. The snapshot and enclosing strategy-result hashes differ
because the Engine WindowManager's end boundary is the replay horizon and the
strategy trace includes the snapshot hash; do not treat those envelope hashes
as opening-value parity.

The real auction evidence remains partial: 09:20 has 1,319/5,222 available
anchors, 09:24 has 3,422/5,222, and 09:25 has 5,068/5,222; a RecoveryPlan for
the 154 missing 09:25 anchors was requested but not executed. The current Core
runner did not apply the available 09:26 producer sidecar, and no 09:32 or
09:40 producer snapshot sidecar was supplied. The upstream per-frame T1
acquisition manifest was also not retained with this artifact set. Thus this
closes full-window Core consumption for the frozen artifact, not all planned
cutoff comparisons or Phase 4 provenance.

Verification: `832 passed` (3 protobuf/upb deprecation warnings), compileall
PASS, diff-check PASS. No external Redis/TD/Rabbit access or writes occurred
during the Core run. `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged; do not promote a successor task.

## 19. Same-date field-delta / producer-cutoff reconciliation — 2026-10-08

The 2026-09-30 full Core Q2Frame report was rerun with both the existing
pressure sidecar and the new field-delta sidecar. Against the previous same-date
report, Q2Frame input SHA, 754 frames / 428,586 updates, one Engine, 758
signals, revision 754, virtual clock, Engine final hash, opening facts/status,
and pressure summary hash are unchanged. The field-delta result is deterministic
and `FACT_ONLY`; details:
`docs/work/handoffs/TASK-008-PLATE-FIELD-DELTA-Q2FRAME-INTEGRATION-20261008.md`.

The retained same-date t1-v2 command artifact records a 09:32:10 atomic cutoff
publication with 5,213 accepted rows, equal to Core's aggregate 5,213 READY
count. This is not per-symbol value parity: the serialized producer payload is
not retained, and the producer reports partial universe authority. No same-date
09:40 producer snapshot was found in retained validation artifacts. Core's 7
PARTIAL facts are exactly the symbols whose Q2 source age exceeds its
60-second freshness threshold, so the counts are consistent with excluding
the stale tail; producer membership cannot be confirmed without payload rows.
This Core report also has no same-date 09:26 barrier sidecar. Do not reconstruct
these points from a later `latest` state.

The 2026-09-29 selected 10-plate input uses 1,321 observed-cohort members;
the frozen same-date map contains 1,412 members for those plates. All 91
map-only symbols are absent from the captured TD cohort. Preserve this as an
observed-cohort sidecar, keep full-market/plate-universe coverage `UNPROVEN`,
and do not silently widen or shrink the denominator.

**Alignment / next use:** this feature is verified for captured same-date
inputs and does not alter Engine or legacy pressure results. Continue
feature-scoped Core development on pinned real Q2/TD evidence. The missing
09:32 payload and 09:40 snapshot limit only those value-parity claims; they
are not blanket gates for unrelated work. No successor task or NORMAL
acceptance is promoted from this result.

## 20. Same-date anchor timing follow-up — 2026-10-08

Whole-second comparison of pinned 2026-09-30 TD auction rows and the same-date
t1-v2 Q2Frame found 925/936 matching positive prices at 09:20, 3,163/3,229 at
09:24, and 5,030/5,030 at 09:25. Every positive TD price occurred somewhere
in its candidate window; 09:20/09:24 candidates continued to change. The
current timeline captured 5,030 available / 190 missing 09:25 anchors at
09:25:06 using data through 09:25:02. At the first frame after the 09:25:30
soft deadline (09:26:00), unrelated Q2 state changed while anchor content and
revision stayed the same. Detailed artifacts and limitations:
`docs/work/handoffs/TASK-008-ANCHOR-TIMING-REAL-DATA-20260930-20261008.md`.

This is event-time replay evidence, not Rabbit arrival or historical
availability. It supports retaining whole-second timing observations without
turning small offsets into hard failure gates. Do not promote NORMAL acceptance
or use missing same-date 09:32/09:40 snapshots as blockers for unrelated Core
features.
