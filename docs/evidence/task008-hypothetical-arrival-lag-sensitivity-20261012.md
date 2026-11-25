# TASK-008 — hypothetical arrival-lag sensitivity

Recorded: 2026-10-12 (Asia/Shanghai)
Result: `DIAGNOSTIC_ONLY`
TASK-008 remains: `PARTIAL_EVIDENCE`

## Question and method

This bounded experiment asks how existing 09:20/09:24/09:25 auction-anchor
facts vary if captured Q2Frame updates become readable after an assumed delay.
It uses `ArrivalLagModelV1` and the existing Core auction-anchor builder. For
each source frame, modeled availability is
`Q2Frame.logical_ts_ms + constant_lag`; source update `ts` and payload fields
are not changed. Reads use a causal right-closed schedule: the first read is
aligned at or after the first source logical time, subsequent reads receive
arrivals in `(previous_read, current_read]`, and configured auction milestones
are added as exact evaluation reads. Empty polling slots do not become market
frames; actual empty source frames are preserved.

This is a sensitivity model, not a measured arrival distribution. In
particular, zero lag means “visible at the captured Q2Frame logical time” in
this model; it does not mean zero Rabbit latency.

For constant lag `L`, a read at `R` is only a query of the same captured
availability curve at `R - L`. The five lag values therefore do not represent
five independently observed delay distributions. The 1s/3s zero-lag
milestone-hash comparison is an implementation consistency check because the
same exact milestone reads are added to both cadence schedules; it is not a
new market-data finding. No additional constant-lag matrix is warranted from
this input.

## Frozen real input and scope

- Input: `/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/run/prefix.q2frame.jsonl`
- SHA-256: `7474de28bfc60e8777cef0f359b4f6d2ab1b4f1ab6bcdf4fee5211a721483467`
- Full captured artifact: 1,190 Q2Frames, 1,146,352 updates, 5,224 distinct
  observed symbols; the source README identifies it as output from the
  deployed t1-v2 replay of a real TD prefix.
- Experiment prefix: every captured frame from the start through source
  logical time 09:25:30 inclusive. No symbol sampling or field filtering.
- Every one of the 10 scenarios processed the same 603 Q2Frames and 195,421
  updates. Input/delivery update fingerprints matched in all scenarios.
- Expected denominator: all 5,224 symbols observed anywhere in this captured
  Q2Frame. This is not a full-market universe claim.
- Lags: 0, 3, 6, 10, and 30 seconds. Read cadences: 1 and 3 seconds.
- The recorded zero-lag exact-milestone anchor-content hashes matched between
  1s and 3s cadence. This is a scheduling self-check, not proof that arbitrary
  cadences or live queue reads are equivalent. No source timestamp regression
  and no update `ts` later than its containing Q2Frame logical time were
  observed in this prefix.

## Available 09:20/09:24/09:25 anchor symbols

Counts are available anchor prices / 5,224 captured-universe symbols. The
cadence-1s and cadence-3s runs had the same reported counts. For
09:20 and 09:24 each cell is `first-observable → soft-deadline → convergence`
(the last time is soft deadline plus the assumed lag). For 09:25 each cell is
`09:25:06 / 09:25:10 / 09:25:30 / convergence`. Cadence 1s and 3s yielded the
same values in every row.

| Assumed lag | 09:20 | 09:24 | 09:25 |
|---:|---:|---:|---:|
| 0s | 1,192 → 3,360 → 3,360 | 3,229 → 4,094 → 4,094 | 4,872 / 4,872 / 4,872 / 4,872 |
| 3s | 678 → 3,360 → 3,360 | 3,054 → 4,094 → 4,094 | 4,872 / 4,872 / 4,872 / 4,872 |
| 6s | 0 → 3,360 → 3,360 | 2,833 → 4,094 → 4,094 | 3,342 / 4,872 / 4,872 / 4,872 |
| 10s | 0 → 3,360 → 3,360 | 301 → 4,094 → 4,094 | 0 / 3,342 / 4,872 / 4,872 |
| 30s | 0 → 678 → 3,360 | 0 → 301 → 4,094 | 0 / 0 / 3,342 / 4,872 |

The 09:25 revisions remain `PARTIAL` against this captured-symbol denominator
because 352 symbols have no positive `a25` in this input. The later modeled
read changes availability without blocking the timeline. This is consistent
with the intended “use what is available, then permit a later revision” shape;
it does not prove that live data arrives with any row's assumed delay.

The runner applies local latest-per-symbol field merging and invokes the Core
anchor-revision builder only. It does **not** execute
`Q2FrameReplaySource`, a `DeterministicEngine`, the `AuctionTimeline` revision
chain, or `AuctionShadowStrategy`; freshness behavior and Engine/strategy
consumption were not tested by this experiment.

## Provenance limits and side effects

- The input is t1-v2 Q2Frame output from TD replay, not a captured Rabbit
  `DataBatch` or queue delivery log. This run did not access Redis, TDengine,
  RabbitMQ, production logs, or systemd.
- `rabbit_arrival=UNKNOWN`, `source_sequence=UNKNOWN`, and
  `historical_available_at=UNKNOWN_NOT_INFERRED`.
- The model order is synthetic and deterministic under the explicit lag and
  source-frame/update order. It is not historical arrival order.
- `PER_UPDATE_HASH` is only unit-tested as a deterministic stress mode; it was
  not run against this real Q2Frame prefix and is not an empirical lag model.
- No producer, ACK, persistence, Redis key, TD write, effect, or service state
  was changed. Production side effects: `NONE_OBSERVED`.
- `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` are unchanged. This
  experiment does not establish NORMAL opening equivalence.

Full scenario output and its checksum list:
`/home/exedev/validation/task008-arrival-lag-sensitivity-20261012T150426+0800/`.
Both `arrival_lag_sensitivity.json` and `report.md` passed `sha256sum -c`.
After the scan, `report.md` received scope/cadence wording clarifications and
its checksum was updated; the scenario JSON and measured values were not
rewritten.

## Implementation and verification

- Base development HEAD: `b38bba6ab669f9e1308a4bb20efa9add75d3e97b`
- `src/engine_core/replay_arrival.py` SHA-256:
  `5ad2a0f699a78eb749ae8cfc5a490a05e02aa32e14f851107f90909ff3e246f4`
- Executed scan runner SHA-256:
  `a270d4ba981db2cda7abf1fa136d2195a4b74dda667dc65013f94eca8d0e3727`
- Current reusable runner SHA-256 (report-scope wording clarified after the
  scan; simulation logic unchanged):
  `333509ca1ac8ba533752a4533efd0a72ec25ad23b0805cc2bc774e5c5e19085b`
- `tests/test_replay_arrival.py` SHA-256:
  `6f05944402c6329e4138964e8018cdb155aac8c588adfc9b42498c68b72ac9f7`
- `src/engine_core/__init__.py` SHA-256:
  `8280eb167d0107e9845661fa262fb05571c0a41bfd51486173878ad8db591ab9`
- Targeted arrival-model tests: 10 passed.
- Full test suite: 1,042 passed (three dependency deprecation warnings).
- `compileall`: PASS; `git diff --check`: PASS.

No task acceptance status is promoted by this diagnostic experiment.
