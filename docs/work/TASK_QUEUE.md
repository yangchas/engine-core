# Task Queue

## RUNNING

### TASK-008

- title: Real opening validation (production read-only; isolated replay allowed)
- owner: `replay-investigator`
- state: `PARTIAL_EVIDENCE`
- replay_for_development: `USABLE_WITH_LIMITS`
- normal_opening_acceptance: `UNPROVEN`
- branch: `codex/feature-session-engine-integration`
- worktree: current development worktree
- plan: `docs/work/plans/TASK-008-opening-validation.md`
- handoff: `docs/work/handoffs/TASK-008-REAL-DATA-20260920.md`
- audit: `docs/work/handoffs/TASK-008-AUDIT-20260920.md`
- replay_handoff: `docs/work/handoffs/TASK-008-REPLAY-REAL-DATA-20260920.md`
- validation_dir: `/home/exedev/validation/task008-opening-validation-20260920T183000+0800/`
- latest_current_code_replay:
  `/home/exedev/validation/task008-current-source-replay-20261008T173904+0800/core_q2frame_report.json`
- latest_full_window_current_source_replay:
  `/home/exedev/validation/task008-current-source-full-window-20261008T181726+0800/core_full_window_report.json`
  Replayed the pinned 2026-09-23 producer artifact through the complete
  `[09:15:00,09:40:00)` timeline: 500 frames / 1,209,672 updates, 5,222
  symbols, ordered/repeat fully deterministic, final hash unchanged from the
  earlier full-window run. This is current Core consumption of captured
  t1-v2 output, not a new TD/Rabbit or upstream t1-v2 execution. 09:32/09:40
  producer cutoff snapshots are absent; TASK-008 remains partial.
- post_open_live_observation_20261009:
  `/home/exedev/validation/open-live-followup-20261009T094227+0800/`
  Read-only capture executed at 09:42:27, after the open. It observed stale
  Q2 data, stable `a20/a24/a25` values between 09:30 and 09:42, and t1-v2
  `wall_lag_ms=188800` by 09:42:59 (`ack_fail=0`); lag cause and TD write
  health remain UNKNOWN/UNPROVEN. Same-input local Core projection and probe
  hashes repeated, but this is not Rabbit parity or NORMAL opening acceptance.
  See the offline transition audit below for the earlier full-cohort changes
  and the sampling/causality limits.
  The replay process stayed stopped; after-close action is to revalidate its
  PID/offset, input hashes, source mtimes, and t1-v2 lag before deciding
  resume versus restart. Related scope notes:
  `docs/work/handoffs/TASK-008-FRAME-GAP-SCOPE-ROBUSTNESS-20261009.md` and
  `docs/work/handoffs/TASK-008-PARTIAL-REASON-DIAGNOSTICS-20261009.md`.
  Offline audit of the nine 09:14–09:30 real Redis captures found positive
  `a20` rewrites (212) and positive `a24` rewrites (708), alongside fills;
  five-way full observed-cohort counts show zero positive-to-empty clears.
  All `a20/a24/a25` values were identical by symbol from 09:25:06 to 09:30 and
  from 09:30 to 09:42; the 09:30:13–09:42:27 interval itself was unsampled.
  These observations do not locate changes before/after event-window close or
  prove late arrival. The 20-symbol all-Shenzhen capture fixture's before/after
  rows and source hashes were independently verified; it covers revision
  update behavior, not market-wide rates. Focused regression, timeline file,
  and opening-strategy file pass (1, 53, and 10 tests). The paused replay was
  rechecked at 10:43 (PID 3831221, still T; offset 339,845,120 / 342,636,341;
  input hashes unchanged) and was not resumed during market hours. This does
  not provide NORMAL acceptance. Full counts, hashes, freeze timestamps,
  runtime/source caveats, and limitations are in
  `/home/exedev/validation/open-live-followup-20261009T094227+0800/offline_anchor_transition_audit.md`.
- previous_field_delta_current_code_replay:
  `/home/exedev/validation/task008-field-delta-same-date-audit-20261008T044210+0800/core_q2frame_with_pressure_and_field_delta_20260930.json`
- prior_current_code_replay_without_field_delta:
  `/home/exedev/validation/task008-0930-current-core-replay-20261008T015810+0800/core_q2frame_report.json`
- latest_replay_handoff:
  `docs/work/handoffs/TASK-008-CURRENT-WORKTREE-ROBUSTNESS-RERUN-20261008.md`
- previous_feature_handoff:
  `docs/work/handoffs/TASK-008-PLATE-FIELD-DELTA-Q2FRAME-INTEGRATION-20261008.md`
- generic_q2frame_streaming_repeat_audit:
  `docs/work/handoffs/TASK-008-GENERIC-Q2FRAME-STREAMING-REPEAT-20261008.md`
  The generic helper now drains only through the current whole-second frame
  group and supports an explicit end horizon for pending timers. The frozen
  2026-09-30 artifact repeated deterministically (758 frames / 434,188
  updates); this closes only the helper subtask and does not change TASK-008's
  `PARTIAL_EVIDENCE` or NORMAL acceptance status.
- previous_current_same_date_q2frame_handoff:
  `docs/work/handoffs/TASK-008-CURRENT-SAME-DATE-Q2FRAME-REPLAY-20261008.md`
- previous_full_context_current_code_replay:
  `/home/exedev/validation/task008-0929-current-core-replay-20261008T005739+0800/core_q2frame_report.json`
- previous_full_context_handoff:
  `docs/work/handoffs/TASK-008-CURRENT-CODE-FULL-REPLAY-20261008.md`
- latest_replay_result: current Core source completed two deterministic
  one-Engine passes through 09:32:10 on hash-pinned real 2026-09-30 Q2Frame,
  same-date pressure context, and field-delta context. The input, final state
  hash, and the common pre-existing fact/count/status fields match the previous
  same-date baseline. The report contract changed V12→V16 and this run adds a
  field-delta context absent from that baseline. Engine snapshot/result and
  per-symbol evidence hash/reference fields differ; their exact cause is not
  inferred. The deterministic field-delta summary is
  `FACT_ONLY`. Producer cutoff metadata also reports
  5,213 accepted rows at 09:32:10, matching Core's aggregate READY count, but
  payload rows are absent, so this is not per-symbol value parity. Core's 7
  PARTIAL facts are exactly the 7 symbols older than its 60-second freshness
  policy at that cutoff; the count agreement is consistent with excluding the
  stale tail but does not prove identical producer membership. 09:20/09:24
  adjacent-delta `fact_status_counts` are `PENDING`, but standalone anchor
  facts are present (09:20: 1,197 available; 09:24: 3,309 available). Direct
  same-date TD comparison finds observed differences for those optional tags;
  the 09:25 anchor matches 5,030/5,030 positive prices and 190/190 missing
  rows. These differences are not hard gates on 09:25 analysis. Rabbit arrival,
  historical `available_at`, producer barrier parity, and full-market coverage
  remain unproven.
- current_worktree_robustness_rerun: matching 2026-09-30 real Q2Frame,
  pressure context, and field-delta context were rerun from current HEAD
  `06c3366` plus the recorded dirty source diff. Two passes completed
  deterministically (754 frames / 428,586 updates); 09:32 produced 5,213 READY
  and 7 PARTIAL facts, and standalone 09:25 anchors were 5,030 AVAILABLE /
  190 MISSING. Common pre-existing non-hash outputs match the previous
  same-date report; its V12 contract and lack of field-delta context are
  explicit scope differences. Evidence hash/reference fields differ and are
  not claimed equivalent. Runtime was about 35 minutes, recorded but not a
  gate. Subsequent recovery fixes cover unknown-universe fills and isolate a
  default-zero response against a `None` primary anchor; the latter uses the
  pinned real Q2Frame fixture for the primary row, but the recovery response is
  a contract test rather than captured Wencai output. Sparse recovery responses
  now restore omitted old symbols/fields from the saved primary cohort, with
  diagnostics. Conflicting rows are quarantined per symbol and do not discard
  valid sibling fills; if all fills are quarantined, recovery remains required
  and is recorded as `ERROR`. A follow-up isolates out-of-plan symbols/fields
  and unreported fills while retaining valid siblings. Plan/date/revision
  envelope errors remain hard failures; an embedded symbol/key mismatch is
  quarantined only for that member. A stale idempotent retry now returns the
  current timeline revision instead of an older result
  revision. Embedded identity mismatch now quarantines one member; the latest
  malformed fill/diagnostic symbol declarations are now quarantined as
  member-level anomalies instead of aborting valid siblings. If all declared
  members are invalid, a diagnosed response records `ERROR`/`PARTIAL` and
  remains retryable; clean empty `APPLIED` still fails validation. A malformed
  expected-universe symbol is likewise quarantined without losing valid
  observed anchors; its denominator stays unknown and revision stays PARTIAL.
  Full Core suite is now 933 passed. Recovery remains
  contract-tested only; no live Wencai
  response or Redis/TD/Rabbit integration was tested. The current-source
  Q2Frame replay has since been rerun after these source changes. Its new report
  is byte-identical to the preceding same-input report, SHA-256
  `5330060220fb47998d78030725698289befe3ba36072911febf73c11831c7d0e`; both
  reports record 754 frames / 428,586 updates, identical final state hash, and
  all deterministic checks true. This confirms no regression on that real
  captured input, not execution of recovery edge cases or live integration.
  Rerun report:
  `/home/exedev/validation/task008-current-source-replay-20261008T173904+0800/core_q2frame_report.json`.
  Handoff:
  `docs/work/handoffs/TASK-008-CURRENT-WORKTREE-ROBUSTNESS-RERUN-20261008.md`.
- latest_field_delta_raw_audit:
  `/home/exedev/validation/task008-field-delta-same-date-audit-20261008T044210+0800/raw_audit_20260930.json`
- latest_same_date_anchor_timing_audit:
  `docs/work/handoffs/TASK-008-ANCHOR-TIMING-REAL-DATA-20260930-20261008.md`
- latest_same_date_anchor_timing_artifacts:
  `/home/exedev/validation/task008-anchor-candidate-alignment-20260930-20261008T060542+0800/`
  and `/home/exedev/validation/task008-anchor-revision-scope-20260930-20261008T060542+0800/`
- latest_direct_core_anchor_td_reconciliation_all_tags:
  `examples/audit_task008_core_anchor_td_snapshot.py` compares all three
  current-Core anchors to same-date TD rows. 09:20: 925 equal positive values,
  11 positive-value differences, and 260 Core-positive/TD-NULL cases; 09:24:
  3,163 equal positive values, 66 positive-value differences, and 80
  Core-positive/TD-NULL cases; 09:25: 5,030/5,030 positive values and
  190/190 missing rows match exactly. Early-tag differences are diagnostic,
  not a gate on 09:25; timestamp semantics and arrival latency are not inferred.
  Evidence: `/home/exedev/validation/task008-core-anchor-td-snapshot-all-tags-20260930-20261008T064902+0800/`.
- latest_same_date_anchor_q2_field_alignment:
  handoff `docs/work/handoffs/TASK-008-ANCHOR-Q2-FIELD-ALIGNMENT-20261008.md`;
  source-time candidate output
  `/home/exedev/validation/task008-anchor-q2-field-alignment-final-20260930-20261008T062642+0800/`;
  plate-impact sensitivity output
  `/home/exedev/validation/task008-anchor-q2-plate-impact-final-20260930-20261008T063147-v2/`;
  per-plate partitions reconcile within the mapped cohort, but each anchor has
  69 unmapped TD symbols and full-market/production plate parity remains
  unproven; active release uses weighted multi-plate IntradayContext inputs
  that are absent from the sealed Q2Frame/map, so this is not production
  auction-bucket parity; diagnostics only, not timing or coverage gates
- same_date_0932_producer_command_capture:
  `/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/deployed_release_auction_commands_to_0932.jsonl`
- same_date_0940_producer_snapshot: `NOT_FOUND_IN_RETAINED_2026-09-30_ARTIFACTS`
- result: live Redis read was `PARTIAL`/`STALE_OR_MIXED`; target-date
  2026-09-18 frozen Q2 replay completed with ordered/shuffled deterministic
  hashes equal, but 5,219 source rows are future relative to 09:32:10, so
  replay remains `REPLAY_PARTIAL`
- producer boundary: Q2 is generated by `t1-v2`; the prior TD-only replay is
  tick-layer evidence and does not prove Q2-dependent opening behavior
- bridge evidence (completed): exact current `t1-v2 --replay --q2frame`
  followed by Core Q2Frame replay; no Redis/TD writes. The full real window
  consumed 1,224,811 TD rows and emitted 1,205 source-time batches, because
  t1-v2 groups equal `tss`; it is intentionally not one Q2Frame per 3-second
  read slice. Core repeat replay is `REPLAY_READY_BOUNDED` with deterministic
  hashes equal.
- strict Q2Frame evidence: `PASS` as a separate 500 contiguous slice-boundary
  contract check; it is not Rabbit delivery or exact t1-v2 batch equivalence.
- exact full-window bridge evidence:
  `/home/exedev/validation/td-rabbit-phase-c-q2-full-20260924T014403+0800/`
- TD/Rabbit global replay plan:
  `docs/work/plans/TD_RABBIT_GLOBAL_3S_REPLAY.md`
- canonical sliced-read revalidation after ordering fix:
  `/home/exedev/validation/td-rabbit-phase-b-full-canonical-20260924T192010+0800/`
- current phase status: `PHASE_P_PARTIAL`; exact TD→t1-v2→Core Q2 bridge is
  verified for the full window, and isolated Redis DB9/DB11/DB12/DB13/DB14
  projection plus the 09:25:06 empty-slice Clock are verified. 09:25 price
  and change fields match comparable TD snapshot rows, while amount/rest fields
  remain PARTIAL and require same-version field-semantic evidence. Rabbit
  delivery equivalence, 09:25:06 live visibility, historical available_at
  and `TD_WRITE_HEALTH` remain `UNKNOWN/UNPROVEN`
- development-use basis: real 2026-09-24 TD→exact t1-v2→isolated Redis DB7/8→Core
  replay processed 429,392 rows; both runs had identical normalized semantic
  keys, 5,222 Q2 hashes, and matching ordered/shuffled Core hashes. The result
  remains `PARTIAL` with 24 stale quotes. Unknown historical `available_at`
  limits point-in-time claims but does not block replay-led feature development.
- current Redis cutoff probe: `/home/exedev/validation/task008-real-redis-audit-20260925-qlzb4egc/`;
  this is a later read of date-scoped active membership plus global Q2 hashes,
  so it is a source/runner diagnostic and not a historical 09:32 cohort.
- mainline_next: continue development against pinned real TD/t1-v2/Q2 replay
  evidence; retain Rabbit timing and NORMAL opening as separately unproven.
- phase_p_auction_close_empty_slice: current t1-v2 commit `9472f4c` was
  regression-tested against real 2026-09-23 TD `[09:15:00,09:26:03)`. The
  preceding `[09:25:57,09:26:00)` frame was empty; the next frame had five
  `09:26:00.000` rows. Fixing the empty-frame right-edge timestamp and adding
  an auction-close barrier changed the bounded `latest` summary from the stale
  0925 amount `13,619,535,240` to `13,621,401,036`, equal to Q2 `am` sum and
  the release control. All 5,222 Q2 hashes and frozen 0920/0924/0925 outputs
  stayed identical. Self-tests passed; the run wrote only to isolated Redis
  DB15 (`td_sql=0`, `ack=0`). This closes one current-source defect only;
  Phase P stays `PARTIAL` and no later stage is started. Handoff:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_AUCTION_CLOSE_EMPTY_SLICE_20260925.md`;
  detailed validation:
  `/home/exedev/validation/t1v2-latest-boundary-repro-20260925/auction_close_boundary_report.md`.
- phase_p_barrier_experiment: validation-only barrier-aware whole-slice binary
  replayed real 2026-09-24 `[09:15:00,09:25:09)` into Redis DB13/
  `task009pbarrier:`. It processed 210730 ticks in 204 batches plus one Clock;
  Q2 and 0920/0924/0925/latest/0925-anchor semantic content matched the exact
  Phase M baseline, and Core readback was deterministic (`5222/5222`, coverage
  `1.0`, 549 stale, `FACT_ONLY`). This is `REAL_VALIDATION_PASS_WITH_LIMITS`,
  not Rabbit delivery equivalence or NORMAL acceptance. Evidence:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_EXPERIMENT_20260925.md` and
  `/home/exedev/validation/td-rabbit-phase-p-barrier-run-20260925T010000+0800/`
- phase_p_barrier23_experiment: the same validation-only barrier-aware binary was
  re-run on real 2026-09-23 `[09:15:00,09:25:09)` into Redis DB14/
  `task009pbarrier23:`. It issued one SELECT per 3-second slice (203 slices,
  212022 rows, one empty slice), produced 204 t1-v2 batches plus one Clock, and
  recorded `td_sql=0`/`ack=0`. Q2, legacy auction, anchor and A2 semantic
  content matched the exact DB5/`task009k:` baseline; Core readback was
  deterministic at coverage `1.0`, missing `0`, stale `154`, `FACT_ONLY`. This
  is the second real-date validation of the barrier hypothesis, not Rabbit
  delivery equivalence or NORMAL acceptance. Evidence:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER23_EXPERIMENT_20260925.md`.
- phase_p_barrier_corrected_current_source: the current t1-v2 development source
  was built and real-replayed three times on 2026-09-23
  `[09:15:00,09:25:09)` into three unique Redis DB15 prefixes. Successful runs
  processed `212022` ticks with `source_reject=0`, `td_sql=0`, `ack=0`; all three
  normalized semantic Redis hashes match after excluding only global
  `m2:runtime.redis_bytes` (`ea53a68d...`), and DB0 prefix hits are zero. Core
  readback is `5222/5222`, coverage `1.0`, missing `0`, stale `154`, `PARTIAL`.
  Important correction: output does **not** fully match the old DB5 baseline:
  5206 Q2 hashes differ, 0920/0924 counts each differ by one, while 0925 count is
  equal; root cause remains UNKNOWN. The previous validation-only parity result
  must not be attributed to this current-source build. Still `PHASE_P_PARTIAL`;
  do not advance to Rabbit/live or NORMAL claims. Full audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_CORRECTED_AUDIT_20260925.md`.
- source_alignment_audit: read-only comparison found current development
  `e91a20a` calculation semantics differ from deployed release source in
  auction matching/rest amount, auction limit reference and Engine clock/session
  handling. This plausibly confounds the Q2/A2 deltas but is not yet a proven
  full cause. A controlled validation hybrid has now used release calculation
  files with the current 3-second reader/barrier on real 2026-09-23 data:
  all 5222 Q2 hashes match the DB5 baseline, but the 0920/0924 auction snapshots
  each differ by one member and their ranked/summary outputs differ; 0925 and
  its anchor match. Phase P remains `PARTIAL`. Do not attribute the residual
  delta to one cause until barrier membership is compared. Audits:
  `docs/work/handoffs/TD_T1V2_SOURCE_ALIGNMENT_AUDIT_20260925.md` and
  `docs/work/handoffs/TD_T1V2_SOURCE_ALIGNED_HYBRID_REPLAY_20260925.md`.
- phase_p_barrier_trace_followup: a later current-source replay on the same
  real date/window, with opt-in per-symbol barrier capture, now matches DB5/
  `task009k:` for all 5222 Q2 hashes, the active set, all A2/legacy 0920/0924/
  0925 projections, and the 0925 anchor. Trace counts are 4873/5100/5208 at
  09:20:03/09:24:10/09:25:06; no future source timestamps were included. This
  supersedes the mismatch conclusion for the earlier `e91a20a` build only and
  is bounded to 2026-09-23 `[09:15:00,09:25:09)`. Phase P/TASK-008 remain
  partial because Rabbit delivery/arrival and historical `available_at` are
  still unproven. t1-v2 trace/test commit `5c61f43` is local only, not pushed or
  deployed. Audit: `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_TRACE_AUDIT_20260925.md`.
- Phase G real Redis write evidence: isolated DB15/`task009g:` replayed
  09:20:00–09:25:09 from real TD through the current t1-v2 release; 129281
  ticks, 305 batches, one empty-slice Clock, 260710 Redis commands, `td_sql=0`,
  `ack=0`, 5209 Q2 hashes and 0920/0924/0925 A2 outputs. A repeat on DB10
  with `task009g2:` had identical normalized semantic content; Core read back
  5209 real Q2 hashes with ordered/shuffled projection hashes equal. DB0 stayed
  at 12269 keys and had no task prefix. This is `PHASE_G_PARTIAL`, not normal
  opening acceptance.
- phase_e_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_E_AUDIT_20260924.md`
- phase_e_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-e-redis-20260924T193920+0800-barrier/`
- phase_f_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_F_AUDIT_20260924.md`
- phase_f_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-f-0924-0925-20260924T194854+0800/`
- phase_g_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_G_AUDIT_20260924.md`
- phase_g_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-g-0920-0925-20260924T200214940+0800/`
- phase_i_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_I_AUDIT_20260924.md`
- phase_i_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-i-0923-0924-0925-20260924T201547736+0800/`
- phase_i_cross_day_repeat: real 2026-09-23 09:24:00–09:25:09 TD replay through
  the current t1-v2 release wrote isolated Redis DB9/DB8. Both runs processed
  44276 ticks in 64 batches with one clock, `td_sql=0`, `ack=0`, and 5215
  prefixed keys. Normalized semantic content matched exactly. Core read 5206
  Q2 hashes with ordered/shuffled projection equality. 0925 snapshot had 5222
  rows, including 16 symbols absent from this TD window; this is a real set
  difference, not an automatic producer failure.
- phase_k_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_K_AUDIT_20260924.md`
- phase_k_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202721825+0800/`
- phase_k_baseline_closure: extending the same real date to 09:15–09:25:09
  processed 212022 ticks in 604 batches with one Clock, wrote 428284 Redis
  commands to isolated DB5, and produced 5222 Q2 symbols. DB4 repeat had
  identical normalized semantic content. TD source/Q2/0925 snapshot symbol
  sets were all 5222; the prior 16-symbol difference was caused by starting
  replay at 09:20 instead of preserving the 09:15 baseline. Core readback was
  deterministic but remains `REPLAY_PARTIAL` because 17 baseline quotes are
  stale under the explicit 10-second freshness policy. The 14 Q2 symbols not
  present in the A2 anchor correspond to old snapshot rows with null price/
  change and zero auction amounts; they are unavailable facts, not Q2 loss.
  For the remaining 5208 A2 symbols, chg/match/rest bid/rest ask matched
  5208/5208 and comparable price matched 5068/5068; 140 NULL-price rows retain
  explicit unavailable semantics. A2 numeric field parity is therefore
  `PARTIAL_WITH_EXPLICIT_UNAVAILABLE`, not a fabricated full PASS.
- phase_l_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_L_AUDIT_20260924.md`
- live_log_arrival_audit:
  `docs/work/handoffs/TD_RABBIT_LIVE_LOG_AUDIT_20260924.md` (progress/ACK
  observed; delivery membership, arrival order and watermark remain UNKNOWN)
- phase_l_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-l-0923-full-20260924T203942122+0800/`
- phase_l_full_window: exact current t1-v2 replay of real TD
  `09:15:00–09:40:00` processed 1204178 ticks in 1224 batches with 78
  clocks, wrote 2430075 Redis commands to isolated DB3, and emitted 5222 Q2
  symbols plus 0920/0924/0925 anchors. DB2 repeat had identical normalized
  semantic content. Frozen 0920/0924/0925/anchor keys matched the shorter
  09:25 cutoff run, proving later rolling Q2 did not overwrite them. Full-window
  Core readback is now complete: the final 5222-Q2 capture had coverage=1.0,
  equal ordered/shuffled projection hashes and equal sampled Engine hashes.
  Core classified it as `REPLAY_PARTIAL` because 68 quotes were stale under
  the explicit 10-second freshness policy; this is deterministic real-data
  evidence, not NORMAL opening acceptance. This aggregate quality label does
  not stop replay or discard the other quotes: stale symbols remain identified
  individually and unaffected facts continue to be produced.
  The Core readback's 1,209,672 updates come from a separately retained Q2Frame
  artifact whose upstream build identity is unknown; it is not proven to be
  the output of this Phase L run. Do not compare its update count to Phase L's
  1,204,178 source rows as a same-run loss/gain check or a blocking gate.
- strict_q2frame_validation_dir:
  `/home/exedev/validation/task008-q2frame-real-20260923/`
- phase_m_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_M_AUDIT_20260924.md`
- phase_m_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-m-0924-0915-0925-20260924T210300+0800/`
- phase_m_same_day_cutoff: exact current t1-v2 replay of real 2026-09-24
  `09:15:00–09:25:09` processed 210730 ticks in 604 batches plus one
  09:25:06 Clock, wrote 425676 Redis commands to isolated DB1/`task009m:` and
  recorded `td_sql=0`, `ack=0`. Core readback covered 5222 Q2 hashes and was
  ordered/shuffled deterministic; 549 quotes were stale under the 10-second
  policy, so the result remains `REPLAY_PARTIAL`. DB0 had zero task009m keys.
- phase_m_repeat: DB6/`task009m2:` repeated the same window with identical
  210730/604/1/425676/`td_sql=0`/`ack=0` counters; normalized semantic SHA
  `17d8f3f176116d8578559713fcfcf0d9d5e95294f74e00b3c4e85356acd3c28f` matched
  DB1. Only Redis runtime byte/counter metrics differed.
- phase_m_barrier_facts: real 2026-09-24 TD had 4681 rows in 09:25:00–03,
  one row in 09:25:03–06, and zero rows in 09:25:06–09. DB1 Q2 max source ts
  was 09:25:03 while the frozen anchor meta ts was 09:25:06. This proves the
  observed-date empty-Clock barrier path; a real 05/06/07 mixed delivery is
  not present and remains `UNVERIFIED`.
- phase_m_replay_cutoff_fact: the old exact-release scheduler needed the
  `[09:25:06,09:25:09)` query to observe its empty control slice. The current
  development reader includes a barrier at the right edge of
  `[09:25:03,09:25:06)` and emits its Clock there; the old statement is
  historical, not current-source behavior. See the corrected source audit.
- phase_m_cross_day_barrier_search: TD read-only search across 2026-09-18
  through 2026-09-24 found zero rows in `[09:25:06,09:25:09)` for every date;
  a real mixed 05/06/07 sample therefore requires Rabbit capture or a future
  TD date and remains `UNVERIFIED`.
- phase_n_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_N_AUDIT_20260924.md`
- phase_n_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-n-0924-0915-0932-20260924T221500+0800/`
- phase_n_real_opening_bridge: exact current t1-v2 replay of real
  `2026-09-24 09:15:00–09:32:09` wrote only isolated Redis DB7/`task009n:`;
  it processed 429392 ticks in 753 batches with 78 clocks, 878448 Redis
  commands, `td_sql=0`, and `ack=0`. Core directly read 5222 Q2 hashes at
  historical observation `09:32:09`, coverage=1.0, stale=24 under the
  explicit 10-second policy, and equal ordered/shuffled plus sampled Engine
  hashes. This is `REPLAY_PARTIAL`, not NORMAL acceptance; DB0 had zero
  `task009n:*` keys.
- phase_n_repeat: the same real TD window was replayed through the same
  t1-v2 binary into isolated Redis DB8/`task009n2:`. Counters were identical
  (`429392` ticks, `753` batches, `78` clocks, `878448` Redis commands,
  `td_sql=0`, `ack=0`). DB7/DB8 normalized semantic Redis content had 5232
  keys each, difference count `0`, and identical SHA
  `c88639da305a303221c8ea9ca900060a816551bcf0385244aadb62f854050a05` after
  excluding runtime metrics. DB0 remained untouched.
- phase_o_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_O_AUDIT_20260924.md`
- phase_o_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-o-0924-full-20260924T224351+0800/`

- phase_p_audit:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_AUDIT_20260924.md`
- phase_p_validation_dir:
  `/home/exedev/validation/td-rabbit-phase-p-single-slice-20260924T232137629+0800/`
- phase_p_single_slice_fact: exact current t1-v2 replay with
  `REPLAY_BATCH_SIZE=1000000` still emitted 1224 timestamp-group batches for
  1194572 real ticks; Q2/auction semantic content matched Phase O and Core was
  deterministic, but Rabbit delivery-shape equivalence and the no-compute-chunk
  contract remain `UNPROVEN/NOT_CLOSED`.
- phase_p_bounded_slice_validation:
  `/home/exedev/validation/td-rabbit-phase-p-single-0925-20260924T235445283+0800/`
  proves one real `[09:25:00,09:25:03)` slice (4681 rows) emitted three
  `tss`-group batches (`3174/739/768`) despite the large batch-size setting.
- phase_o_full_window: exact current t1-v2 replay of real 2026-09-24
  `09:15:00–09:40:00` wrote only isolated Redis DB9/`task009o:`. Independent
  TD count and t1-v2 `source_in/ticks` were both 1,194,572; the run produced
  1,224 batches, 78 clocks, 2,408,279 Redis commands, `td_sql=0`, and `ack=0`.
  Core read 5,222 Q2 hashes with coverage 1.0 and equal ordered/shuffled plus
  sampled Engine hashes. 116 quotes were stale under the explicit 10-second
  policy, so the result is `PHASE_O_PARTIAL` / `REPLAY_PARTIAL`, not NORMAL
  acceptance. Stable 0920/0924/0925 legacy auction projections matched prior
  isolated runs; DB0 had zero `task009o:*` keys.
- rabbit_contract_audit:
  `docs/work/handoffs/TD_RABBIT_CONTRACT_AUDIT_20260924.md`
- rabbit_contract_status: exact release schema and RawTick fields are
  observed; Core RabbitFixtureAdapter is implemented, but t1-v2 C++ TickBatch
  currently does not propagate Rabbit `batch_id`, DataBatch `sent_at`, or wire
  `record_count`. Per-tick arrival, delivery sequence and completion watermark
  are absent from the schema and remain UNKNOWN. No production C++ or
  consumer/ACK change was made.
- replay_validation_dir: `/home/exedev/validation/task008-replay-opening-20260918T185727+0800-v2/`
- normal_opening_pass: `UNPROVEN`

## MERGED

### TASK-007

- title: Offline canonical replay / auction facts
- owner: `replay-investigator`
- state: `MERGED` (independent auditor PASS)
- branch: `codex/feature-session-engine-integration`
- worktree: current development worktree
- started_at: `2026-09-20T14:10:36+08:00`
- plan: `docs/work/plans/TASK-007-offline-canonical-auction-facts.md`
- scope: pure Rabbit-primary canonical batch to replay/facts seam
- implementation_commit: `084d6819b31a80087d624cfabf0d78843c8613ba`
- latest_fix_commit: `aa38614` (`state` moved to timing evidence; same auction
  source revision keeps semantic identity)
- handoff: `docs/work/handoffs/TASK-007-offline-canonical-audit.md`
- fix_handoff: `docs/work/handoffs/TASK-007-FIX-20260920.md`
- tester_handoff: `docs/work/handoffs/TASK-007-TESTER-20260920.md`
- real_data_audit: `docs/work/handoffs/TASK-007-REAL-DATA-AUDIT-20260920.md`
- readonly_audit: `docs/work/handoffs/TASK-007-READONLY-AUDIT-20260920.md`
- independent_auditor: `docs/work/handoffs/TASK-007-INDEPENDENT-AUDIT-20260920.md`
- integrated_by: `INTEGRATOR_REVIEW_TASK007_20260920.md`
- implementation_tests_before_fix: `668 passed`
- current_tests: `682 passed`, compileall PASS, diff-check PASS
- offline_tester: `PASS`; independent auditor: `PASS`
- real_data_ordered: `PASS (completed)`; validation:
  `/home/exedev/validation/task007-real-ordered-20260920T154654+0800/`
- real_data_functional: `PASS` (500 canonical frames completed with no side
  effects)
- real_data_performance: `OPTIMIZATION_REQUIRED` (18.07 min for 500 canonical
  frames; not a functional replay failure)
- performance_variant: `FINAL` hot-path optimization completed; latest real
  ordered run 13.53 min with final session hash parity
- performance_evidence:
  `/home/exedev/validation/task007-perf-final-500-hotpath-20260920T172951+0800/`
- real_data_determinism: 500-frame ordered/shuffled `PASS`; validation:
  `/home/exedev/validation/task007-real-shuffled-20260920T161558+0800/`
- production_side_effects: `NONE_OBSERVED`
- merge_recommendation: `MERGE`
- next_task: TASK-008 remains the current mainline; feature-scoped Core work may
  continue by explicit invocation when its own inputs are available. Unproven
  NORMAL/live evidence is not a project-wide development gate, and no task is
  auto-started.

Merge gate (all satisfied):

- canonical batch conversion is deterministic and missing-safe;
- degraded `BLOCKED`/`PARTIAL` frame diagnostics reach Engine evidence;
- batch quality and order ambiguity are not discarded;
- identical auction content advances timing evidence without fabricating a revision;
- no Rabbit/TD/Redis/Wencai/effect import or write path is added;
- ordered/shuffled frame hashes agree;
- empty frames and optional auction anchors remain explicit;
- recovery revision idempotency and late correction evidence pass;
- full pytest, compileall and diff-check pass;
- integrator review is recorded before feature-branch merge.
- independent auditor reports `AUDIT_STATUS=PASS` and no blocking findings.

### TASK-006

- title: Unified Rabbit-primary canonical tick/batch contract
- owner: `replay-investigator`
- state: `MERGED`
- implementation_commit: `272cbd7428aaf8ac205759ec7d6afa5f230814a8`
- integrated_by: `INTEGRATOR_REVIEW_20260920.md`
- branch: `codex/feature-session-engine-integration`
- tests: `660 passed`, compileall PASS, diff-check PASS
- production_side_effects: `NONE_OBSERVED`

### TASK-005

- title: Replay session timeline integration
- owner: `replay-investigator`
- state: `MERGED`
- implementation: `ReplaySessionTimeline` hash-only ledger
- branch: `codex/feature-session-engine-integration`
- tests: `660 passed`, compileall PASS, diff-check PASS
- production_side_effects: `NONE_OBSERVED`

### TASK-004

- title: Cross-sectional replay performance closure
- owner: `replay-investigator`
- state: `MERGED_WITH_WARN`
- branch: `codex/feature-session-engine-integration`
- result: ordered FRAME/FINAL and both FRAME deterministic evidence completed;
  full passes remain in the 5–10 minute `PASS_WITH_WARN` band
- production_side_effects: `NONE_OBSERVED`

## BLOCKED

### TASK-003

- title: Robust cross-sectional replay foundation
- state: `BLOCKED_BY_PERFORMANCE`
- commit: `2de51f5`
- tests: `623 passed`
- evidence: `/home/exedev/validation/replay-20260918-performance-20260920T101233+0800-final`
- production_side_effects: `NONE_OBSERVED`

## MERGED (historical)

### TASK-001

- title: Real-data replay audit for 2026-09-18 09:15-09:40
- owner: `replay-investigator`
- state: `MERGED`
- started_at: `2026-09-20T01:47:07+08:00`
- current_commit: `9f7c2a3`
- branch: `codex/task-real-data-replay-20260918`
- worktree: `../engine-core-replay-20260918`
- execution: bounded read-only audit completed; evidence and review gates passed
- final_status: `REPLAY_READY_BOUNDED`
- validation_dir: `/home/exedev/validation/replay-20260918-0915-0940-20260920T014707+0800`
- tester: `PASS`
- auditor: `PASS`
- current M3-1 status: `BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`

Merge gate:

- replay inputs are read-only and have a manifest;
- bounded replay completes without production side effects;
- two or more runs have identical deterministic hashes;
- event, slice, signal, engine, and final hashes are traceable;
- `UNKNOWN` values, time semantics, availability, and source limits are explicit;
- auditor recommends merge only after tester evidence is complete.

## BACKLOG

All other migration, ownership, strategy, checkpoint, Rabbit, and effect tasks
remain deferred. No card is auto-promoted by this bootstrap.
