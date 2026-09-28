# TASK-008 — Q2 auction-anchor Core shadow handoff

Date: 2026-09-29
Result: `REAL_REPLAY_VERIFIED_WITH_LIMITS`

## Change

The Core Q2 adapter now exposes t1-v2 `a20/a24/a25` as separate canonical
auction-anchor fields. The TASK-008 Q2Frame Engine shadow explicitly reads
those fields for auction price facts and leaves price missing when an anchor is
unavailable; it does not fall back to the changing latest `px`. This is a Core
consumer-path change only. It does not modify t1-v2 or producer/Redis/TD/Rabbit
paths.

## Evidence

The current V2 runner completed against the frozen real Q2Frame input with SHA
`08187d216274180e407565463f4ea482442748f7b70e57d5568418bd35518262`.
Validation directory:

`/home/exedev/validation/task008-core-anchor-semantic-20260929T042935+0800/`

The output identifies `Task008Q2FrameAuctionEngineShadowV2`, the explicit
`0920→a20`, `0924→a24`, `0925→a25` mapping, and no-fallback behavior. Two runs
matched exactly. Core processed 202 frames / 225,829 updates through 09:25:06
with one Engine; the source inventory has 500 frames and 5,221 symbols. The
remainder through 09:40 was not processed by this feature-scoped runner.

On the same real input, 3,472 per-symbol price deltas equal the previous V1
result; 1,749 V1 values become missing in V2, with zero contradictory
non-null values. This is expected when the legacy path used latest `px` but
the corresponding auction anchor is unavailable. It is not a blanket failure
or a claim that every symbol has a usable anchor.

Existing independent read-only TD evidence shows the 4,408 non-NULL 0925
snapshot prices match Q2Frame `a25` at event-time sequences 201 and 202. The
persisted 0925 amount/rest snapshot is a mixed per-symbol cohort. Rabbit
arrival order and historical availability remain unknown.

## Status and limits

- Anchor-field routing in this bounded real Core replay: verified.
- Determinism: PASS.
- 09:25 facts: `PARTIAL`; 09:20/09:24 strategy facts remain `PENDING` until
  the composed 09:25 fact evaluation.
- Auction revision `READY` means symbol membership coverage against the
  Q2Frame-derived expected universe only; it does not prove field completeness
  or full-market coverage.
- Full `[09:15,09:40)` Core replay: not established by this run.
- Rabbit arrival/membership, historical `available_at`, and NORMAL opening
  acceptance: `UNKNOWN` / `UNPROVEN`.
- TASK-008 remains `PARTIAL_EVIDENCE`; this handoff does not advance the task.
- Production side effects: `NONE_OBSERVED`.

Full audit and checksums are in the validation directory above. The task
specific code/test diff was verified with 711 total tests, 43 focused tests,
compileall, and diff-check. Commit the scoped code/tests and this handoff only;
do not stage unrelated dirty project-planning documents.
