# TASK-008 Core full-window continuation audit — 2026-10-07

> Follow-up: the 09:26 Core-vs-T1 passive checkpoint comparison requested at
> the end of this 2026-10-07 audit was completed on 2026-10-08 against the same
> SHA-pinned 2026-09-23 producer artifacts. It matched 5,222/5,222 canonical
> Q2 values. See
> `TASK-008-0926-CORE-PASSIVE-CHECKPOINT-20261008.md`. TASK-008 remains
> `PARTIAL_EVIDENCE`; 09:32/09:40 producer sidecars were not found in the
> retained barrier artifacts.

## Result

The current Core Q2Frame runner can continue the same in-memory
`DeterministicEngine` after `OPENING_0932` through the last frame in the frozen
2026-09-23 input. The real frozen input completed all 500 frames in both the
ordered run and its repeat, with identical final state hashes. This closes the
specific former 343-frame cutoff gap; it does not close TASK-008 as a whole.

```text
CORE_FULL_WINDOW_CONTINUATION=PASS_WITH_LIMITS
TASK_008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Frozen inputs and provenance

Validation directory:
`/home/exedev/validation/task008-current-head-full-replay-20261007-CJLfBg/`

| Artifact | Size | SHA-256 |
|---|---:|---|
| `slice-q2frames.jsonl` | 342,636,341 bytes | `c6317dcf444ca4b54c36cba43713294ccdf04ba325c1e2abb603c29b67d5a1a4` |
| `barrier-q2frames.jsonl` | 5,546,374 bytes | `91daad1c6ec2236796e25b6ce4e5fcf66a077d58c2d1159e3e8e3754b145cc58` |
| prior `core-opening.json` | 42,244,545 bytes | `0a7ae4de1c5cc2434676eef64d009080210e0c87e028fde631151870521d0e86` |
| new `core-through-input.json` | 42,244,415 bytes | `e81ed19b45f7ffe43c4899d8dacc8ae7486520901d83061cb9e42102b9a08206` |

The Q2Frame artifact is described by its manifest as output from a real
2026-09-23 TD → t1-v2 replay path. Separately, the Phase L run record reports
500 sequential three-second slices over `[09:15:00,09:40:00)`, 1,204,178 TD
rows, zero rejected source rows, 5,222 symbols, and 78 empty frames. The Q2Frame
file contains 500 frames and 1,209,672 per-symbol updates. Its producer build
identity and linkage to that Phase L run were not preserved. The 5,494
arithmetic difference is therefore not established as a same-run discrepancy,
and is not evidence of dropped or extra source rows. The upstream
stdout/per-frame count-and-digest manifest was not retained, so the Q2Frame's
T1 acquisition accounting cannot be independently reconstructed from these
artifacts. This provenance gap is recorded, not used as a replay stop gate.

Core ran from branch `codex/feature-session-engine-integration`, base HEAD
`a18654e10af663c48dc0571c5baa6c457baebaf0`. The worktree was already dirty;
the tested source was not committed. At run time the runner SHA-256 was
`86eb1994b30be5093902146e989649e18d4837ea9a7f79a5eabeb0f7d527ce2e` and the
targeted test file SHA-256 was
`efd626084fbdfad1be3f6f3684565a81b3218ef3350e005c3900599f52ad879c`.
Do not attribute this result to the base commit alone.

## Run and observed results

The runner read only the two pinned local Q2Frame artifacts and ran ordered
plus repeat passes. It did not reconnect to TD, Redis, or Rabbit during this
Core run and did not write any external system.

```text
trade_date=2026-09-23
window=[09:15:00,09:40:00)
frames=500 (422 non-empty, 78 empty)
symbols=5,222
Q2Frame updates=1,209,672
engine_instances=1 per pass
first_excluded_frame=null
processed_signals=507 per pass
reducer_revision=503 per pass
VirtualClock=09:39:59 +08:00 (last input event in the half-open window)
termination=THROUGH_FINAL_INPUT_FRAME
ordered final_state_hash=7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d
repeat final_state_hash=7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d
deterministic=true
```

The 09:20, 09:24, and 09:25:06 producer barrier sidecars were applied with
5,222 updates each. The source sidecar also contains a 09:26 producer audit
record, but the current Core auction runner intentionally ignores non-auction
tags; it was not applied as a Core barrier.

Observed auction-anchor availability remained soft/partial, not a stop:

| Anchor | Available | Missing | Result |
|---|---:|---:|---|
| 09:20 | 1,319 / 5,222 | 3,903 | `PARTIAL` |
| 09:24 | 3,422 / 5,222 | 1,800 | `PARTIAL` |
| 09:25 | 5,068 / 5,222 | 154 | `PARTIAL`; recovery plan `REQUESTED`, not executed by Core |

The opening calculation at 09:32:10 remained `5,210 READY / 12 PARTIAL` within
the observed Q2Frame cohort. Full-market coverage and historical availability
remain unproven. The runner reports `NORMAL_OPENING_ACCEPTANCE=NOT_EVALUATED`.

## Opening-fact comparison against the prior 343-frame run

At the same 09:32:10 evaluation, both artifacts include the same first 343
frames / 436,327 updates. The following hashes match exactly:

```text
facts_by_symbol_hash             514aeae2738a1ed2cdf8725959b7e70f84e6416737961886c310c385a644138b
opening_fact_field_status_hash   4a00be2371710d7e574f71b5e0565de8aea9133b3075904db9230a4a14d88aa9
cross_section_facts              b2bc03138b25527f179f213fe00efdad9be63984b334c040bf5a5a44fc16cbb1
opening transition facts         3032d97fac2da9028e1697d7c5207505b312a6df539213befafcdc8e596f183f
amount summary                   4f02a3b65d3663deaceeda49767e9b84af242bcbb05d5dd57b5ac6dd0e6ea3f8
limit summary                    8ddb790214be9fe1ceaadcc30247e874efea20465afb032e2dee019dd139adab
```

The full-window `engine_snapshot_hash` and `strategy_result_hash` differ from
the opening-only run. This is expected under the current contract: the
Engine's `WindowManager` is initialized with a different replay end, the
snapshot hash includes that window view, and the opening strategy trace
includes the snapshot hash. The fact and summary hashes above are the relevant
opening-value comparison; the two run-envelope hashes are not interchangeable
with those fact hashes.

## Remaining gaps and alignment

- No Core comparison was made against the 09:26 producer sidecar; the Core
  runner reports that tag as `NOT_PROVIDED` because it is not an auction
  anchor. No producer snapshot sidecars were supplied for 09:32 or 09:40.
  The 09:32 result is a Core calculation over streamed Q2Frames, not producer
  snapshot parity; the 09:40 result is a final Engine state, not a separately
  frozen producer cutoff projection.
- This was ordered input plus an identical ordered repeat, not an
  ordered-versus-shuffled test. It proves deterministic repetition for these
  frozen bytes, not order independence or Rabbit delivery/arrival equivalence.
- The Core process consumed local Q2Frame files, not Redis. This validates the
  producer-artifact → Core computation path, not a fresh Redis read adapter
  or live queue integration.
- The opening result remains `PARTIAL`; the 154 missing 09:25 anchors generated
  a recovery request but no recovery provider was run. This is not a reason to
  block calculations that do not depend on those fields.
- No independent second-agent audit was run. The local audit checked the
  pinned inputs, same-engine count, counters, hashes, missingness, status, and
  side-effect boundary.

Verification after the implementation:

```text
pytest: 832 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
```

The run was read-only with respect to Redis/TD/Rabbit and services. During
monitoring, `engine-next` and `t1-v2-live` were observed `active`; no service
restart or production mutation was initiated. No commit, push, merge, or
deployment was performed.

**Alignment:** full same-engine Core consumption is now demonstrated for the
frozen 500-frame producer artifact. TASK-008 stays `PARTIAL_EVIDENCE` because
cutoff producer/Core comparisons, source per-frame acquisition provenance,
and broader replay-for-development coverage remain incomplete. Keep
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` unchanged.

The next smallest useful check is to reconcile the already-present 09:26
sidecar with the Core event-time state at that timestamp, then determine
whether same-date producer snapshots for 09:32/09:40 already exist. Reuse
existing evidence first; if absent, report the gap rather than substituting a
post-run latest value or inventing a historical snapshot.

## Follow-up — current-source full-window replay — 2026-10-08

The same SHA-pinned `slice-q2frames.jsonl` and `barrier-q2frames.jsonl` were
rerun through current Core source `06c33666114c3d5971f8060aa43d02dfdfc5a9b7`
plus the then-current dirty diff. Both file hashes still match the inputs
above. The current report contract is V15; ordered/repeat each processed all
500 frames / 1,209,672 updates, 507 signals, reducer revision 503, and ended
at VirtualClock 09:39:59. All determinism checks are true and the final state
hash is the same as the previous full-window report:
`7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d`.
Opening remains 5,210 READY / 12 PARTIAL; 09:25 anchor remains 5,068
AVAILABLE / 154 MISSING. The 09:20/09:24 adjacent deltas are PENDING, but
standalone 09:25 and opening facts continue to be produced.

Current report:
`/home/exedev/validation/task008-current-source-full-window-20261008T181726+0800/core_full_window_report.json`
(SHA-256 `accf5d294809dcfb1b246c914bcf80fe0b39656b843b4c494e1d06533347c65d`).
Runtime was approximately 26.5 minutes, not a gate. The 1,204,178 upstream TD
row count vs 1,209,672 Q2 update count remains unreconciled because the
per-frame T1 manifest is missing; no loss/invention is inferred. This proves
current Core replay against captured t1-v2 output only—not fresh TD, Rabbit,
or t1-v2 execution. No historical 09:32/09:40 producer cutoff snapshots were
found in the supplied artifacts, so full-window producer parity remains
unproven. No Redis/TD/Rabbit/service access, writes, or deployment occurred.
