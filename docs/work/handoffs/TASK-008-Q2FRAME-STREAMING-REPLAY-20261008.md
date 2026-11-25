# TASK-008 — Q2Frame streaming helper repair

Date: 2026-10-08 (Asia/Shanghai)

## Finding and change

`src/engine_core/replay.py::replay_q2frames()` previously materialized every
input frame into a `signals` list before submitting any signal to the Engine.
That contradicted the repository's time-sliced replay rule. The helper now
iterates the source, submits only the current logical-time group, and drains
that group at the next timestamp boundary. It parses one next frame to detect
the boundary but does not apply that frame to the source before the prior
group is drained. Frames sharing a timestamp remain grouped and preserve the
prior ordering behavior.

The TASK-008-specific file runner already streamed its JSONL input; this patch
repairs the separate reusable Core helper.

## Real-input verification

Input:

`/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/deployed_release_q2frame_to_0932.jsonl`

SHA-256:

`1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`

One pass through the repaired helper and a real `DeterministicEngine` consumed
the complete sealed file:

- 758 frames, 434,188 Q2 updates, 5,220 observed symbols;
- 758 Engine signals and 758 reducer revisions;
- 758 logical-time groups; largest group contained one signal;
- final source sequence: 758; last file frame: 09:32:14 Shanghai;
- final reducer state hash:
  `0f7f53081d0943cdcf15752d7bdd8f03a7507cea6cf3335e77ac3c5238d156b5`.

This full-file streaming check is not the TASK-008 09:32:10 cutoff report: the
sealed file includes frames through 09:32:14. It does not establish live Redis
visibility, Rabbit arrival order, historical `available_at`, or NORMAL
opening equivalence.

## Tests and boundaries

- Regression test proves the helper does not exhaust the iterator before
  draining prior logical-time groups and preserves same-time grouping.
- Full suite: 870 passed (three existing protobuf/upb deprecation warnings).
- `compileall` and `git diff --check`: PASS.
- No live Redis, TDengine, RabbitMQ, service, or production write access.

This closes only eager materialization in the generic helper. TASK-008 remains
`PARTIAL_EVIDENCE`; `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`.
