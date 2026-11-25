# TASK-008 current-worktree robustness rerun — 2026-10-08

## Result

The current Core worktree completed a deterministic two-pass replay over the
sealed real 2026-09-30 t1-v2 Q2Frame and same-date TD-derived contexts.
Isolated stale/missing facts remained local; the run completed without a
global data-completeness gate.

```text
CURRENT_WORKTREE_Q2FRAME_REPLAY=PASS_DETERMINISTIC_WITH_LIMITS
INDIVIDUAL_QUALITY_DEGRADATION=PASS
FULL_MARKET_COVERAGE=UNPROVEN
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Inputs and source identity

- Q2Frame: `deployed_release_q2frame_to_0932.jsonl`, SHA-256
  `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`.
- 2026-09-30 auction-pressure context file SHA-256
  `a6e483750a7b10da15e8b3cc55c108c46c51ce2505051459dc01d0303f6e56d5`.
- 2026-09-30 field-delta context file SHA-256
  `8b2ca8e2d107d1d971d3eba7eb06564bce5b57b7c67326034f3b95ad26b502b5`.
- Core branch/HEAD: `codex/feature-session-engine-integration` /
  `06c33666114c3d5971f8060aa43d02dfdfc5a9b7`.
- Worktree was dirty; the tracked diff SHA-256 was
  `13dab3ba462fccdbadf29ba623dc6ff5fd465c122b6304b33f0447d7098f7819`.
  Python package manifest SHA-256:
  `8b5a684778960437309fc2bc369f27632116f2ad48c25bf8dc78c690d2613e94`;
  runner SHA-256:
  `edbfaf026f7bebab5759a45b24459d82396b2bb6b37d504d2b9b4cf94dcbca7c`.

The first invocation selected 2026-09-29 sidecars for a 2026-09-30 replay and
was rejected by the trade-date contract before producing a report. The
sidecars were checked and replaced with the matching 2026-09-30 files; the
successful result below uses only date-matched inputs. This was an invocation
correction, not a source-data failure.

## Replay findings

- Contract `Task008Q2FrameSessionEngineShadowV16`; decision remains `FACT_ONLY`.
- Ordered and repeat passes each processed 754 frames / 428,586 updates, with
  758 signals, reducer revision 754, and identical final state hash
  `78fe99285be0a6b60a455ea7f15d9b37568fdf62bf6c5675734b5ed3fc4f0dc4`.
- Determinism report: `true`; no comparison check was false.
- 09:25 standalone anchor facts: 5,030 `AVAILABLE`, 190 `MISSING`.
  The separate adjacent-comparison fact contract reports 5,211 `PARTIAL` and
  9 `MISSING`; these statuses are not a gate on the standalone 09:25 anchor.
- 09:32 opening cohort: 5,213 `READY`, 7 `PARTIAL`, zero missing; the seven
  stale observations remain visible and do not stop the cohort.
- Compared with the preceding same-date report, the common pre-existing
  non-hash values/counts/statuses and final state hash matched. The report
  contract changed from V12 to V16, and this run adds a field-delta context
  absent from that baseline. Engine snapshot/result and per-symbol evidence
  hash/reference fields differed; the exact cause is not inferred from that
  comparison. Within this current-source run, ordered/repeat hashes are
  deterministic.
- Runtime was approximately 35 minutes. This is recorded as an observation,
  not a failure or acceptance gate.

Evidence report:
`/home/exedev/validation/task008-current-source-robustness-audit-20261008T151100+0800/core_q2frame_report.json`
(SHA-256
`5330060220fb47998d78030725698289befe3ba36072911febf73c11831c7d0e`).

Current-worktree verification: `909 passed` (three dependency deprecation
warnings), `compileall` passed, and `git diff --check` passed. The run read
sealed local files only; no Redis, TDengine, RabbitMQ, service control, or
production write occurred. Historical Rabbit arrival and `available_at`
remain `UNKNOWN`; full-market and NORMAL opening acceptance remain unproven.
This evidence does not change `M3_1_NORMAL=BLOCKED` or
`TD_WRITE_HEALTH=UNPROVEN`.

## Post-replay recovery contract fix — 2026-10-08

After this full replay, a targeted audit found that a 09:25 recovery plan
created with no known expected-symbol universe could not accept its first
returned cohort: the plan correctly carried an empty symbol list and a missing
anchor field, but validation treated the empty universe as a prohibition on
new symbols. The revision contract had the same empty-vs-unknown assumption.
The path now accepts only the result-declared filled symbols and requested
anchor field when no universe is known, retains `PARTIAL`/`FACT_ONLY`, keeps
coverage unknown, and does not automatically request the same unbounded
recovery again after an `APPLIED` result when no residual universe is
knowable.

Regression evidence uses pinned real Q2Frame fixture
`q2frame_0925_real_limit_states_20260930.json` (SHA-256
`10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`), not a
handwritten market price. The fixture proves the in-memory recovery contract
against a real captured anchor value; it does not prove a live Wencai/Redis
recovery integration. Full suite: `910 passed`; compileall and diff-check
passed. No live data source or production service was accessed. This source
fix was made after the replay above; no claim is made that the 35-minute
Q2Frame replay exercised it.

## Follow-up — isolate out-of-scope recovery data — 2026-10-08

The recovery-result boundary previously rejected the whole cohort when one
row carried an out-of-plan symbol/field or an undeclared anchor fill. The
plan-bound path now quarantines that symbol/field/fill, preserves the saved
primary value, and continues valid declared sibling fills. If no requested
fill survives, it records `ERROR` while retaining the usable `PARTIAL` facts
and recovery-required state. Repeated idempotent application preserves the
original result state and diagnostics.

A delayed retry after a newer independent source revision now returns that
current timeline revision instead of returning an older revision object that
could be mistaken for current state. It does not append a revision or replay
the old fill; the original recovery revision stays in timeline history.

Regression coverage includes a valid fill alongside an extra field, an
out-of-plan declared symbol and field, an undeclared fill, a response with no
valid requested field, and an embedded symbol/key mismatch that must still
hard-fail. Focused timeline tests: 47 passed; full suite: 926 passed;
compileall and diff-check pass. Source identity and revision failures remain
hard errors. The recovery response is synthetic contract input; pinned real
Q2Frame data supplies the primary baseline only. No live Wencai response,
Redis/TD/Rabbit connection, production write, service action, or replay was
performed. This does not change TASK-008's `PARTIAL_EVIDENCE` status or prove
NORMAL opening acceptance.

## Follow-up — rerun current source after robustness fixes — 2026-10-08

The same 2026-09-30 Q2Frame and TD-derived contexts were rerun through the
current Core source after the recovery/universe changes above. Input hashes
were unchanged; the new report is byte-identical to the earlier report from
this handoff (SHA-256
`5330060220fb47998d78030725698289befe3ba36072911febf73c11831c7d0e`). Both
ordered/repeat passes have 754 frames / 428,586 updates, 758 signals, reducer
revision 754, virtual clock 09:32:10, and final state hash
`78fe99285be0a6b60a455ea7f15d9b37568fdf62bf6c5675734b5ed3fc4f0dc4`; all 12
determinism comparisons are true. Opening remains 5,213 READY / 7 PARTIAL;
standalone 09:25 anchors remain 5,030 AVAILABLE / 190 MISSING. Adjacent 09:20
and 09:24 delta facts remain pending, but do not block the independent 09:25
anchor or opening facts.

The full file inventory contains 758 frames / 434,188 updates across 5,220
symbols; the opening cutoff consumed 754 frames / 428,586 updates and excluded
post-09:32:10 frames. This is a same-input current-Core regression check, not a
rerun of live TD or upstream t1-v2 generation and not a test of Wencai
recovery. Historical arrival/availability and full-market coverage remain
unproven. Runtime was approximately 35 minutes, recorded only as an
observation. No production service, Redis, TD, Rabbit, or write path was
accessed.

New report:
`/home/exedev/validation/task008-current-source-replay-20261008T173904+0800/core_q2frame_report.json`
