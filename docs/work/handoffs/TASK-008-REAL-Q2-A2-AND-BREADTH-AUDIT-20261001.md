# TASK-008 real Q2 → A2 and opening breadth audit

Audit date: 2026-10-01 (Asia/Shanghai)
Scope: continue Core facts validation using pinned outputs of real TD → t1-v2
replays. This was a local artifact audit: no live Redis/TD/Rabbit connection,
no production writes, and no service actions.

## Why this bounded check

The Q2Frame → A2 shadow projection had matched the 2026-09-30 captured Redis
summary. To avoid generalizing from one date, this audit checked a second real
date against the A2 command journal emitted by the same pinned t1-v2 replay.
It also checked the existing 09:32 cross-section calculation against the
same-run `opening_cutoff_v1` producer payload. The comparison is for calculation
alignment, not a reconstructed Rabbit/live cohort.

## 2026-09-29 Q2 → A2 replay comparison

Input artifacts from
`/home/exedev/validation/task008-q2-replay-20260929T150654+0800/`:

- `deployed_release_q2frame_corrected.jsonl`, SHA-256
  `24a5181241cbe1ddb9f33af011cb33f4eabbb7fc041e244e202bec1e6e9112fa`.
- `deployed_release_auction_commands.jsonl`, SHA-256
  `31ace485ed413f2ac78ac3ea29b62138d464a1953794f5336cf843589fa8f0b9`.
- The prior release audit identifies the replay executable SHA-256 as
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.

At the `09:25:06` logical cutoff, Core consumed 603 frames / 193,983 Q2 updates,
with 5,223 latest observed symbols. The last Q2 frame was `09:25:02`, four
seconds before the A2 summary anchor. Using the existing `ANY_POSITIVE(am,br,ar)`
rule:

- the Q2-derived 5,205-member candidate set exactly matched the same-run
  t1-v2 A2 anchor membership;
- all 10 summary fields matched the same-run t1-v2 `0925` summary;
- per-symbol `amount` matched 5,205/5,205, `bid_amount` 5,205/5,205, and
  comparable `change_pct` 5,070/5,070, with zero mismatches.

The four-second source/capture gap is retained as context, not treated as a
failure. This is same-run internal consistency, not an independent live Redis
oracle. A separate 2026-09-29 replay-vs-live Redis audit recorded real
differences; this result does not explain or erase them. That causal difference
remains `UNKNOWN`.

## 2026-09-29 09:32 cross-section comparison

Inputs from
`/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/`:

- `deployed_release_q2frame_to_0932.jsonl`, SHA-256
  `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`.
- `deployed_release_auction_commands_to_0932.jsonl`, SHA-256
  `3422c51fe911172cef247a0eb23ad98f7987a34325abc83db943bebf76f5993f`.
- The producer cutoff payload hash is
  `97c95133a15e1bb6f1c1f125fe65dacdd1b4f16a71a6a524410c027f3f5af04d`.

At the `09:32:10` cutoff, 734 frames / 418,759 Q2 updates produced a latest
observed cohort of 5,223 symbols. The existing `CrossSectionFactsV1` function
reported scope `OBSERVED_COHORT` (not full market), 5,223/5,223 observed facts,
field denominators 5,223 for price and previous close, and breadth:

```text
up=3166, down=1716, flat=341, unknown=0
```

The existing 60-second freshness policy classifies 5,211 symbols as fresh and
12 as stale. The 5,211 fresh-symbol set exactly matched the same-run t1-v2
`opening_cutoff_v1` payload; `price_milli` and `pre_close_milli` matched on all
5,211 shared symbols. Fresh-cohort and producer-cutoff breadth both recomputed
to:

```text
up=3166, down=1716, flat=329, unknown=0
```

All 12 stale facts in this capture were flat; including them changes the
observed-cohort `flat_count` by 12. The runner already reports stale count and
that breadth includes stale observations. This is a bounded quality fact, not
a reason to reject the replay, drop those facts, or impose a new global gate.
The producer payload itself marks universe authority `partial` and has no
`universe_asof_ts`; neither cohort proves full-market coverage.

The generated numerical evidence is in
`/home/exedev/validation/task008-q2-a2-same-replay-20261001T032851+0800/`:

- `cross_date_comparison.json`
- `opening_breadth_audit.json`
- `generated_artifact_sha256sums.txt`

## Audit and alignment

The two real replay dates support the Q2 → A2 projection on the observed
inputs; the 2026-09-30 comparison is against an independent frozen Redis A2
capture, while 2026-09-29 here is same-run producer-output consistency. The
09:32 cross-section calculation also agrees with the same-run fresh producer
cutoff, with stale observations preserved and visible.

No calculation defect requiring a code change was found in this bounded
check. Do not change the candidate rule or freshness behavior from these
results. Do not infer Rabbit arrival order, historical `available_at`, live
visibility, or full-market coverage. Keep:

```text
Q2_A2_REPLAY_PROJECTION=PASS_WITH_LIMITS_FOR_OBSERVED_INPUTS
OPENING_CROSS_SECTION_ALIGNMENT=PASS_WITH_LIMITS
LIVE_REPLAY_EQUIVALENCE=UNPROVEN
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
TASK-008=PARTIAL_EVIDENCE
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

This closes only the current Q2 → A2 / observed opening-breadth audit slice.
The next work should remain feature-scoped Core development against pinned
real replay data; it must not promote TASK-008 to NORMAL acceptance or start
production strategy effects.
