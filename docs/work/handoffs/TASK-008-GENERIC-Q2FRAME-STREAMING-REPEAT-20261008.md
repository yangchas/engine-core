# TASK-008 — generic Q2Frame streaming and timer-horizon audit

Date: 2026-10-08 (Asia/Shanghai)

## Result

```text
GENERIC_Q2FRAME_STREAMING=PASS_WITH_EXPLICIT_HORIZON
REAL_FROZEN_INPUT_REPEAT=DETERMINISTIC_REPEAT_PASS
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

This closes the generic helper's bounded timer-drain behavior and proves two
same-configuration ordered runs on one frozen real Q2Frame artifact. It does
not close TASK-008 or establish Rabbit arrival-order, historical availability,
full-market, or NORMAL equivalence.

## Fix and regression coverage

`DeterministicEngine.run_through()` drains queued signals only through an
inclusive logical-time boundary and advances the replay-owned VirtualClock
immediately before each consumed signal. `replay_q2frames()` uses that bounded
drain at frame boundaries, so a future timer cannot run ahead of unread market
frames. Its optional `end_logical_time_ms` excludes frames after the requested
replay horizon and drains timers through that horizon even when no later market
frame exists. Omitting the parameter retains the existing behavior of ending
at the last input frame.

Regression cases cover timers between frames, at a frame timestamp, later than
the last frame, explicit end-time draining, exclusion of post-horizon frames,
invalid end-time inputs, and subsecond timestamps. A test first reproduced
the bug where frames at `09:25:06.197` and `09:25:06.999` were split around
the `09:25:06` timer. Replay logical times are now floored to whole seconds
before grouping; the source Q2 timestamp remains unchanged, and the timer sees
both same-second updates. The explicit horizon is also floored, so
`09:25:06.999` is the same replay end as `09:25:06.000`. The caller's
VirtualClock must start at or before the first floored replay timestamp.

## Frozen real input and repeat result

Input:

`/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/deployed_release_q2frame_to_0932.jsonl`

```text
input_sha256=1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a
trade_date=2026-09-30
frames=758
updates=434188
observed_symbols=5220
first_logical_ts_ms=1790730900000
last_logical_ts_ms=1790731934000  # 09:32:14 Asia/Shanghai
subsecond_logical_frames=0/758
universe_basis=UNIQUE_SYMBOLS_IN_FROZEN_Q2FRAME_ONLY
```

The audit first scans the JSONL sequentially to inventory the observed symbol
set, then independently streams the file twice through the generic
`replay_q2frames()` helper. It retains the symbol universe, hash chains, and
Engine state; it does not retain all tick/frame rows in memory. Each pass uses
one Engine and a fresh source/clock with identical recorded settings.

| Metric | ordered-1 | ordered-2 |
|---|---:|---:|
| elapsed seconds | 910.314 | 912.620 |
| frames consumed | 758 | 758 |
| processed signals | 758 | 758 |
| reducer revision | 758 | 758 |
| VirtualClock ms | 1790731934000 | 1790731934000 |
| input frame hash chain | `3549b7f45babb872c09849fbed0f9f804b049d0cd91c327e02ec08f6eac3ee8f` | same |
| projection hash chain | `78a11b8ec76fec66f19de5a793fbb1cfd9b701e6f96bddf4bc7a71af83f8fa19` | same |
| final state hash | `57ac02396821a701da3b126b9db5f86a451fc9d9311856bd2401abea1a3c1b7a` | same |
| pending signals after horizon | 0 | 0 |

The prior streaming-helper handoff recorded a different final hash
(`0f7f5308…`). Its Engine/source settings and invocation were not captured
well enough to prove an apples-to-apples comparison. This audit fixes and
records a reproducible configuration; the old hash is therefore
`NOT_COMPARABLE`, not evidence of a current mismatch. A separate earlier
single-run result with another ad-hoc configuration is likewise not used as a
parity oracle.

## Reproduction evidence

Runner:
`examples/audit_q2frame_streaming_replay.py`

Runner SHA-256 at execution:
`bffbf6e04056a8d8ad37cdb7f51e8d36836be1a00acdd2efe0fa2845076b91c9`

Run directory:
`/home/exedev/validation/q2frame-streaming-repeat-20261008T081405+0800/`

Artifacts:

- `replay_summary.json` — input/configuration/code provenance and both run results;
- `replay_audit.md` — concise result and limitations;
- `sha256sums.txt` — hashes for reports and input.

Recorded Engine configuration: one `DeterministicEngine` per pass;
`session_id=STREAMING-Q2FRAME:2026-09-30`, `phase=REPLAY`, window
`[0, 10^16]`, source id `t1_v2_q2frame_streaming_audit`, input order preserved,
explicit replay end at the final input timestamp. The Engine/source implementation
file SHA-256 values and dirty-worktree provenance are in `replay_summary.json`.

The prior cross-turn difference is not resolved as a historical hash
comparison because its original complete configuration is unavailable. The
current exact configuration is repeatable across both runs.

The repeat ran immediately before the whole-second logical-time rule was
added. A post-change sequential inventory confirmed that all 758 input frame
times are exact whole seconds, and the configured replay end is also aligned;
therefore the new truncation is the identity transformation on every logical
time in this frozen input. The subsecond behavior that changed is covered by
the new focused regression test. Only `src/engine_core/replay.py` changed among
the recorded `src/engine_core/*.py` files after the repeat: its run-time SHA-256
was `934fbba55c2ffe08e3686ca09df5b3a3112b12a11f9914b37bdb936351fb4098`, and
the post-change SHA-256 is
`b256e6739fc1ad1442751847a47e4c4bd0d8af5273fa1501c85a1a834885243e`.
After final docstring clarification, current `replay.py` SHA-256 is
`cfa3d90b3fc90e04b26d279b717b7b011572f41e6d3d165c27f2a934dbb80e53`; the
behavioral code delta remains the documented whole-second grouping/horizon.
The audit runner was also hardened after the full run to floor its initial
VirtualClock, report the subsecond-frame count, and emit a directly verifiable
checksum manifest. These audit-only changes do not alter the full input's
replay result. Its run-time SHA-256 is recorded above; the current runner
SHA-256 is `24a6702a21d9cce86603e5793701ea96bf089a9038fd0b47cce90afdcfcf0eb7`.

## Verification and boundaries

```text
pytest: 884 passed, 3 existing protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
validation sha256sum -c: PASS
live Redis/TD/Rabbit/service access: NONE
production writes, ACKs, service controls, effects: NONE
```

The input is a frozen t1-v2-derived Q2Frame artifact, not a live queue capture.
Source-file/event-time order is not Rabbit arrival order. The 5,220-symbol
universe is only the set observed in this artifact; full-market coverage is
unproven. Historical Redis `available_at` remains unknown. Runtime duration is
recorded as an observation, not a failure threshold.

TASK-008 remains `PARTIAL_EVIDENCE`; continue feature-scoped real-data
reconciliation without making second-level timing differences a hard gate.
