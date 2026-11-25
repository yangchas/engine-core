# TASK-008 — real Q2Frame through Core opening handoff

Date: 2026-09-29
Status: `REAL_REPLAY_VERIFIED_WITH_LIMITS`

## Scope and alignment

This is a Core-consumer migration slice: keep the real t1-v2 Q2Frame as the
producer contract, continue one in-memory Core Engine from the auction phase to
the 09:32:10 opening evaluation, and preserve per-symbol source time/quality.
Core does not recompute Q2 from TD. No production data endpoint or service was
accessed or changed; runtime source was only read for contract semantics.

## Implementation

- `OpeningShadowStrategy` now records each symbol's own
  `source_record_time_ms`, not the cross-section's newest timestamp.
- A symbol's fact quality is based on that symbol's required opening values and
  source-time validity/freshness. Aggregate cohort completeness remains
  separately visible and does not degrade unrelated fresh-symbol facts.
- The Q2Frame Engine shadow can continue through `OPENING_0932` at 09:32:10
  without creating another Engine. It applies the 60-second freshness policy as
  a quality label, not as a stop gate; whole-second replay admits same-second
  source timestamps up to 999 ms.
- The first frame after the configured evaluation second is excluded and
  labeled explicitly.

## Real-data evidence

Input artifact:
`/home/exedev/validation/task008-3s-real-replay-20260918-0915-0940-20260926T224518+0800/q2frame.jsonl`

Input SHA-256:
`08187d216274180e407565463f4ea482442748f7b70e57d5568418bd35518262`

Validation directory:
`/home/exedev/validation/task008-q2frame-opening-engine-20260929T053202+0800/`

The artifact inventory is 500 frames / 1,227,873 updates / 5,221 unique
symbols, including 97 empty frames. Core consumed 343 frames / 450,388 updates
through 09:32:10 with one Engine. Frame 344 is 09:32:11 and was excluded.

At 09:32:10, the cohort is `PARTIAL`, with 12 stale and no missing symbols
under the recorded 60-second policy. Per-symbol fact quality is `READY=5209`,
`PARTIAL=12`; each of 5,221 opening fact calculations is present. The
membership coverage of 1.0 is only relative to the frozen Q2Frame-derived
universe, not a full-market claim. The 12 stale codes and per-symbol source
times are in `opening_quality_audit.json`.

Ordered and repeat runs match for all compared evidence and final Engine hash.
The source artifact has zero subsecond updates; real late-arrival behavior and
the live 09:32:10 availability cohort therefore remain unverified.

### Latest rerun and field-preservation audit

The final runner version (`Task008Q2FrameSessionEngineShadowV2`) was rerun
against the same SHA-pinned artifact. Its output is:

`/home/exedev/validation/task008-q2frame-opening-core-20260929T061418+0800/q2frame_session_core.json`

Output SHA-256:
`721caea9e8201a21bed6d131cf2b6631f80c6fba55832436d9e0e4f6614d9d2e`

Field-preservation audit:
`/home/exedev/validation/task008-q2frame-opening-core-20260929T061418+0800/core_q2frame_field_parity.json`

Audit SHA-256:
`cacf3ff42ea3da65be762182131a8a1422d83cc620e95d6616d2a6e8340aa318`

The rerun remained deterministic. One Engine processed 343 frames and 450,388
updates through 09:32:10; reducer revision was 343 and processed signal count
was 347 (including the three auction timers and opening timer). Frame 344,
09:32:11, was excluded. Opening cohort quality was 5,209 `READY`, 12
`PARTIAL`, and zero missing; the 12 stale symbols remain represented rather
than stopping the replay.

A separate read-only comparison reconstructed the latest Q2 update per symbol
from the first 343 frames and compared it with Core's opening facts at the same
cutoff. For all 5,221 symbols there were zero mismatches for `amt2m` →
`amount_2m_yuan`, `ls` → `limit_state`, or per-symbol `ts` → `timestamp_ms`.
Core's `change_pct` also matched `((px / pc) - 1) * 100` for all comparable
symbols (maximum absolute difference 0). This is Q2Frame-to-Core preservation
and formula evidence, not a comparison with a historical Redis snapshot or
Rabbit/live delivery.

The `amount_2m_yuan=AVAILABLE` result concerns the rolling `amt2m` field. It
must not be conflated with the separate auction matched-amount field `am`;
the previously recorded 1,576/5,209 mismatch is `am` versus the TD auction
snapshot, not `amt2m`. `speed_1m` remains `UNKNOWN_UNIT_MAPPING` for all 5,221
symbols because Q2 `spd1m` is in basis points and equivalence to the opening
reader's unit has not been established.

The real frame/source-time delta inventory reports 1,222,021 updates below
3 seconds, 949 from 3 to under 60 seconds, and 4,903 at least 60 seconds
(maximum 902,000 ms); there were no future timestamps or duplicate symbols
within a frame. This is a comparison of each symbol's source timestamp with
the frame logical time, **not** Rabbit arrival or queue latency. The universe
is still derived only from this frozen Q2Frame.

Detailed report:
`/home/exedev/validation/task008-q2frame-opening-engine-20260929T053202+0800/replay_audit.md`

The newer result JSON above supersedes that report for the final runner version
and field-status output; the earlier report remains immutable evidence for its
own run.

## Status and limits

`TASK-008=PARTIAL_EVIDENCE`
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`
`RABBIT_ARRIVAL_ORDER=UNKNOWN`
`HISTORICAL_AVAILABLE_AT=UNKNOWN`
`PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED`

The 09:20/09:24 facts remain `PENDING` and 09:25 facts remain `PARTIAL`. The
auction timeline's `READY` membership state does not mean full-market or
field-complete data. This run does not close TASK-008 or prove live/replay
equivalence.

## Verification

- Core tests after the aggregation/field-quality changes: `717 passed`, 3
  upstream protobuf deprecation warnings
- `compileall`: PASS
- `git diff --check`: PASS

This handoff is scoped to the Core source/tests and must not include the
pre-existing dirty project-planning documents or production diagnostic notes.
