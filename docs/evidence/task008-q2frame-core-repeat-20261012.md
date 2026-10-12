# TASK-008 — full frozen Q2Frame repeat replay

Recorded: 2026-10-12 (Asia/Shanghai)
Result: `REPEAT_DETERMINISM_PASS_FOR_FROZEN_INPUT`
Overall TASK-008: `PARTIAL_EVIDENCE`

## Input and execution

The Core runner consumed the frozen 2026-10-09 Q2Frame at
`/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/run/prefix.q2frame.jsonl`.
Its SHA-256 was independently checked as
`7474de28bfc60e8777cef0f359b4f6d2ab1b4f1ab6bcdf4fee5211a721483467`, matching
the runner's expected hash and the source comparison manifest. The source
README documents that t1-v2 produced this Q2Frame from a real TD prefix with
Redis/TD writes disabled; this Core run itself read only the captured file and
did not reconnect to TD, Redis, RabbitMQ, or a production service.

The runner was invoked with `trade_date="2026-10-09"` and
`continue_through_input=True`, producing `ordered` and `repeat` passes. The
execution worktree was `/home/exedev/repos/engine_core-task008-late-revision`
at HEAD `78a28604f4e3489f31156085f48f8e91eafaa85d`, with the three TASK-008
code/test changes later included in Core commit
`cbd455815bb6450d7f32e468cef036dad9122165`. The executed `src/`, `examples/`,
and `tests/` content matched that commit; the only worktree differences were
two unrelated Markdown hard-break whitespace changes.

## Result

| Measure | Ordered | Repeat |
|---|---:|---:|
| Frames processed | 1,190 | 1,190 |
| Q2 updates processed | 1,146,352 | 1,146,352 |
| Virtual clock end | 2026-10-09 09:39:26 +08:00 | 2026-10-09 09:39:26 +08:00 |
| Final state hash | `61316993dfb529dc900debd25ea79dd52eebe7e917178b5e7e68809d21e5960a` | same |
| 0920 revisions | 18 | 18 |
| 0924 revisions | 11 | 11 |
| 0925 revisions | 1 | 1 |

The inventory began at 09:15:00 and ended at 09:39:26. These are the frame
records present in this captured Q2Frame; the count is not a claim that the
artifact has one frame at every fixed three-second boundary. The runner
reported `deterministic=true` and both passes consumed the full inventory.

The process was observed still running at 1h52m55s elapsed and later exited
successfully. Exact end-to-end duration and phase timing were not captured;
elapsed time is an observation, not a correctness gate.

## Limits and side effects

- This establishes repeat determinism for this frozen Q2Frame through Core. It
  is not an ordered-versus-shuffled comparison, a fresh TD/Redis read, or a
  Rabbit arrival-order replay.
- Only one 0925 revision was emitted. No distinct later 0925 content revision
  was observed in this input; this does not prove that late data was absent
  upstream or that live late-arrival handling is equivalent.
- Rabbit arrival order, historical `available_at`, producer build identity,
  and full-market completeness remain `UNKNOWN`/`UNPROVEN` as applicable.
- During the run, the process had the local input file and standard pipes open;
  observed `/proc` I/O reported `write_bytes=0`. No production side effect was
  observed.

The committed Core tree's full suite passed `1008` tests; `compileall` and
`git diff --check` passed. This code verification does not promote TASK-008 to
complete or change normal-opening acceptance.

Source inventory and production-path limits are documented in
`/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/README.md`
and `compare/manifest.json`. Raw Q2Frame data remains outside Git.
