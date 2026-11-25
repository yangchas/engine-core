# Task009-F: deployed t1-v2 replay of a real TD prefix vs the sealed live Q2 capture (2026-10-09)

Runner: `examples/run_q2frame_vs_capture_fields.py` (offline; consumes a local Q2Frame JSONL and the sealed
capture). Evidence directory:
`/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/` (README, `compare/`, `run/`).

The Q2Frame was produced by the deployed `t1_v2` binary (SHA-256 `363685f8…ab56`) in local-file replay mode
over real TD rows `[09:15:00, 09:39:27)`; Redis/TD writes are forced off in that mode and none occurred.

## Result (5,227 capture symbols; time alignment classified before values)
- SAME_TS 5,061; REPLAY_SOURCE_NEWER 163; NO_REPLAY_STATE 3 (ts=00:00:00 placeholders outside the window).
- Frozen anchors `a20/a24/a25/am/br/ar` and `pc/ph/ls`: 5,224/5,224 MATCH.
- `px/amt/vol/amt2m/amt5m/spd1m/vec3m/vec5m`: no same-ts mismatch; differences exist only where the replay
  source ts is newer than the capture's.
- Same-ts differences: `iv/ia` (~4,100), `ln` (388), `mx` (4,400), `mn` (3,020), `mk` (585, e.g. `kc` vs `sh`).

## Reading the differences
`iv/ia/ln` are latest-event state (see `task009-q2-event-delta-semantics-20261011.md`); `mx/mn` in live are wider
than anything present in the TD rows, and the TD-replay market label differs from the live label for some
symbols. These are input-shape differences between the TD replay path and the live Rabbit path, not shown to be
Core errors. The causes are not established here.

## Limits
One trade date and one capture instant; TD rows and Redis hashes come from the same live process family; no
Rabbit delivery/arrival, `available_at`, 09:25:06 freeze or NORMAL claim.
