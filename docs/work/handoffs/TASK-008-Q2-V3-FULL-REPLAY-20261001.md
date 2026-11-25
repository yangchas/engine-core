# TASK-008 Q2 projection V3 full replay — 2026-10-01

## Result

The current Core Q2 projection, including t1-v2 `iv`/`ia`/`ln`, completed two
identical full replays of the pinned 2026-09-29 t1-v2 Q2Frame artifact.

```text
CORE_Q2FRAME_REPLAY=REPLAY_READY_BOUNDED
Q2_DELTA_FIELDS_REACH_ENGINE_SNAPSHOT=PASS
DETERMINISTIC_REPEAT=PASS
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

This is real historical TD → t1-v2 output replay evidence, not a fresh live
Redis/TD/Rabbit observation and not proof of Rabbit delivery or arrival-order
equivalence.

## Pinned input and command

Input artifact:

```text
/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
```

Its existing lineage audit identifies this as output from the exact-release
TD → t1-v2 replay. This verification itself read only the frozen local file.
It used:

```bash
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/run_task008_t1v2_q2frame_replay.py \
  --q2frame /home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl \
  --trade-date 2026-09-29 \
  --output /home/exedev/validation/task008-q2-v3-replay-20261001T043126+0800/core_q2frame_replay.json
```

Output report SHA-256:
`8de2d39ff2069826acaeeedeaeccdaf9246c469bdb3c15f10e946414a32146fc`.

## Observed replay results

- 735 producer Q2Frame records; 419,533 Q2 updates; 5,223 unique symbols.
- No empty records in this artifact.
- Logical range: 2026-09-29 09:15:00 through 09:32:11 Asia/Shanghai.
- Both passes processed 735 Engine signals and reached reducer revision 735.
- All reported comparisons matched: input SHA, frame hashes, projection hashes,
  processed signals, reducer revision, final state hash, virtual clock and
  symbol coverage.
- Final Engine state hash:
  `fcffff6d78cd2c9d21146e2d9d89eed9fa773dc06c255983bd9fb0e40f95092d`.
- Coverage `5223/5223` is measured only against the unique-symbol cohort in
  this Q2Frame file; it is not full-market coverage.
- The source artifact contained `iv`, `ia`, and `ln` on all 419,533 updates;
  nonzero counts were 202,566 / 202,610 / 33,843 respectively.
- A regression test using the pinned real Q2 update now checks that all three
  typed values reach `EngineSnapshot.symbol_states`; changing only `ln` changes
  the Engine snapshot content hash.

The run took approximately 30 minutes 34 seconds. This is recorded as runner
cost, not a functional failure threshold. The runner currently rebuilds the
full cohort projection for each Q2Frame and has no per-frame heartbeat.

## Limits and alignment

- Q2Frame records are t1-v2 event-time projection groups; they are not asserted
  to be 3-second TD query slices or Rabbit deliveries.
- This proves deterministic Core consumption of this pinned producer output.
  It does not prove historical `available_at`, Rabbit membership/order, live
  Redis visibility, full-market coverage, or NORMAL opening acceptance.
- `iv`/`ia`/`ln` are now preserved through Engine state. No downstream
  engine-next business consumer was found for these normalized fields, so no
  flow aggregate or strategy meaning is inferred or added here.
- The 09:32:11 endpoint is the artifact endpoint, not a 09:32:10 cutoff claim.
- No live data source was contacted; no Redis/TD/Rabbit write, service action,
  production change, or task-board transition occurred.

## Verification

```text
real full Q2Frame replay, two passes: REPLAY_READY_BOUNDED
targeted EngineSnapshot regression: 1 passed
full Core pytest: 759 passed, 3 protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
```

Continue TASK-008 only with a feature that has an evidenced legacy consumer
contract and can be evaluated against pinned real replay input. Do not promote
this bounded result to live parity or NORMAL acceptance.
