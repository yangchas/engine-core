# TASK-008 — auction-anchor field coverage V2

Date: 2026-09-29
Result: `ANCHOR_COVERAGE_V2=PASS_WITH_LIMITS`
Overall TASK-008 remains: `PARTIAL_EVIDENCE`

## Change

`AuctionAnchorRevisionV2` reports source-row coverage separately from
tag-specific anchor-field coverage. A source row without a usable `a20`,
`a24`, or `a25` no longer makes that tag `READY`. The existing Q2 contract
states that a zero-valued auction-anchor price is unavailable; this affects
only that anchor fact, not the rest of the quote or Engine session.

Only a partial/missing `0925` result schedules `RecoveryPlanV1`. Missing
`0920`/`0924` remains optional and does not schedule recovery or block `0925`.
The Q2Frame runner serializes the plan for audit, while explicitly reporting
`NOT_RUN_BY_CORE`; Core performs no external recovery or Redis write.

## Real replay evidence

Input artifact:
`/home/exedev/validation/task008-3s-real-replay-20260918-0915-0940-20260926T224518+0800/q2frame.jsonl`

SHA-256:
`08187d216274180e407565463f4ea482442748f7b70e57d5568418bd35518262`

Run output:
`/home/exedev/validation/task008-q2frame-anchor-v2-20260929T072317+0800/q2frame_auction_core_v2.json`

Audit and checksum manifest:
`/home/exedev/validation/task008-q2frame-anchor-v2-20260929T072317+0800/replay_audit.md`
`/home/exedev/validation/task008-q2frame-anchor-v2-20260929T072317+0800/sha256sums.txt`

The 202-frame / 225,829-update replay reached the 09:25:06 barrier with one
Engine and two identical runs. All 5,221 expected Q2Frame symbols had source
rows. Usable anchors were 1,124/5,221 (`0920`), 3,495/5,221 (`0924`), and
5,171/5,221 (`0925`). The real 0925 result was `PARTIAL`; its recovery plan
requested exactly the 50 symbols whose `a25` anchor was unavailable. No
recovery provider ran. The optional 0920/0924 facts remain `PARTIAL`, do not
schedule recovery, and do not block the 0925 result; per-symbol unavailable
prior anchors cannot be converted into fabricated deltas.

Downstream output also confirms no fallback: all 50 recovery-target symbols
have `price_delta_milli=null` and `changes.price=PRICE_UNKNOWN`. The cohort has
3,472 non-null price-delta metrics, while all 5,221 auction strategy facts
remain `PARTIAL` under their broader fact requirements; this handoff makes no
overall auction-readiness claim.

Earlier oral counts of 1,490 / 3,610 for the first two anchors were incorrect;
streaming the immutable source again produced 1,124 / 3,495, matching the
Core V2 output. This correction is recorded to prevent the stale numbers from
being reused.

## Verification and limits

- Targeted tests: `14 passed`.
- Full Core suite: `721 passed`, with 3 protobuf deprecation warnings.
- `compileall` and `git diff --check`: PASS.
- Production side effects: `NONE_OBSERVED`.
- Rabbit delivery/arrival order, historical `available_at`, live visibility,
  Wencai recovery execution, and NORMAL acceptance remain `UNKNOWN` or
  `UNPROVEN`.
- Coverage is relative to the frozen Q2Frame-derived universe; it is not a
  claim of independent full-market coverage.

This is a bounded Core consumer/replay correction only. It does not complete
TASK-008 or move the project to the next phase.
