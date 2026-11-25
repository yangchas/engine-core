# TASK-008 auction revision content scope — 2026-10-01

Status: focused Core correction `PASS_WITH_LIMITS`; overall
`TASK-008=PARTIAL_EVIDENCE` is unchanged.

## Finding and correction

`AuctionTimeline` previously hashed each full normalized Q2 quote row as
auction-anchor content. A change to ordinary quote fields could therefore
create a new auction revision even when the tag-specific anchor value and its
quality were unchanged. This conflated evolving Q2 observation state with the
content identity of a frozen auction anchor.

The regression was first captured as a failing test. The fix versions the
serialized contract as `AuctionAnchorRevisionV3` and scopes anchor content to
per-symbol anchor value plus field quality. Source/evaluation times and other
observation metadata remain in evidence. Existing Python imports of V1/V2 are
retained as aliases; serialized evidence identifies V3.

## Real-data check

The audit streamed the pinned exact-release t1-v2 Q2Frame artifact and applied
each update to a per-symbol raw state before normalization, matching the
Q2Frame replay source's cumulative update behavior:

```text
input: /home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
input SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
audit: /home/exedev/validation/task008-auction-revision-scope-20261001T153350+0800/auction_revision_scope.json
audit SHA-256: 44b2a0ffcce35a0798baaec03e2d22f9a7b276656685e1babbfb55d234f66c3c
```

At the simulated 09:25:06 observation, the last included source frame was
09:25:02 (seq 603); the anchor cohort was `PARTIAL`, with 5,070 available and
153 missing anchor values among 5,223 observed symbols. The first Q2Frame at
or after the 09:25:30 soft deadline was 09:30:00 (seq 604), 270 seconds after
that deadline. At that cut:

- the unrelated Q2 state changed;
- the full normalized row hash changed;
- the 0925 anchor observation hash and content hash stayed identical;
- the auction revision stayed at 1 and timeline history remained length 1.

The pre-fix identity input was the full normalized row mapping. Its real-data
hash changed from `7d037d8871a9e74ba0d0bd2ee19b9c043415e95533295a89c1e1bb4edc22d42d`
to `8ea5472d9a4081bd552b744761143a800048583ed7d11ca51c204ca22976c416`, while
the anchor observation hash remained
`f0446be1af52b65797d3a66ec1ee56301b1aa3bef72d335d5ea82098f3139e11`. Thus the
old full-row hash input would have changed despite unchanged anchor facts.

This is evidence that the revision no longer changes merely because ordinary
Q2 fields moved. It is **event-time replay evidence only**. It does not prove
Rabbit arrival order, historical Redis visibility, or that a late Rabbit tick
was received after the 09:25:30 soft deadline. The source artifact has a large
time gap from 09:25:02 to 09:30:00, so this audit is not a live-lateness test.

## Verification and boundaries

Verification:

```text
focused auction/runner tests: 23 passed
full workspace pytest: 789 passed, 3 protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production side effects: NONE_OBSERVED
```

The two focused regression cases cover (a) unrelated Q2 field changes without
new anchor revision and (b) anchor quality change creating a revision even
when the anchor value is unavailable. The full suite was run against the
current shared worktree, including pre-existing user edits; those unrelated
files were not modified or staged by this slice.

No Redis, TDengine, RabbitMQ, production service, or live write path was used.
This change does not promote TASK-008 to complete or alter opening acceptance;
`M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` remain outside this slice.
