# TASK-008 — 09:26 passive Core checkpoint audit (2026-10-08)

## Result

The current Core runner passively compared its Q2 reducer state at the 09:26
event second with the same-date T1-v2 barrier sidecar. The sidecar was not
submitted to the Engine. All 5,222 symbols matched under the current comparison
contract, with no duplicate sidecar symbols and no canonical Q2 value/quality
mismatches.

```text
CORE_0926_PASSIVE_Q2_COMPARISON=CANONICAL_Q2_VALUES_EQUAL
CORE_FULL_WINDOW_CONTINUATION=PASS_WITH_LIMITS
TASK_008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

This is a bounded same-date comparison against a frozen real T1-v2 replay
artifact. It is not live Rabbit delivery equivalence, historical
`available_at`, or NORMAL acceptance.

## Inputs and provenance

Frozen artifacts from `/home/exedev/validation/task008-current-head-full-replay-20261007-CJLfBg/`:

| Input | SHA-256 | Scope |
|---|---|---|
| `slice-q2frames.jsonl` | `c6317dcf444ca4b54c36cba43713294ccdf04ba325c1e2abb603c29b67d5a1a4` | Real 2026-09-23 TD → T1-v2 replay output, 500 three-second frames |
| `barrier-q2frames.jsonl` | `91daad1c6ec2236796e25b6ce4e5fcf66a077d58c2d1159e3e8e3754b145cc58` | T1-v2 captured barrier snapshots including 09:26 |

The Core run read these local frozen files only; it did not reconnect to TD,
Redis, or Rabbit. A separate Phase L run record reports 500 slices over
`[09:15:00,09:40:00)`, 1,204,178 TD rows, and zero rejects. This Q2Frame
artifact contains 1,209,672 updates, but its producer build identity and link
to the Phase L run are unknown. The 5,494 arithmetic difference is therefore
not established as a same-run discrepancy, is not evidence of dropped or
extra data, and is not a stop gate.

Core provenance:

```text
branch: codex/feature-session-engine-integration
base HEAD: a18654e10af663c48dc0571c5baa6c457baebaf0
worktree: already dirty; no commit was made for this run
runner SHA-256: 55c025c43292d7c4d2d2b0670345dedd9b197d727129eb61224776ab27f69312
targeted test SHA-256: 0286a82b052f6a4deb485908d43749dee272025ed6160b5e1fc7a23f53be78d8
```

## 09:26 comparison

The passive capture occurred after the full input event-second at 09:26:00 had
been consumed, with no additional Engine signal for the sidecar. The comparison
normalizes both sources to `Q2Quote.to_mapping`; it excludes only observer-time
freshness errors (`stale`, `future_ts`, `trade_date`) from the value comparison,
because those depend on observation policy. Other source parse/validation
errors remain part of the comparison.

```text
checkpoint time: 2026-09-23 09:26:00 +08:00
Core symbols: 5,222
sidecar symbols: 5,222
duplicate sidecar symbols: 0
canonical mismatches: 0
Core / sidecar value hash:
  33e8edb71851aaa0ec32c69de4e0bd2397afa3bea36a1613404a3a405c7d5327
Core observer-time errors: stale=149
sidecar observer-time errors: none
sidecar injected into Engine: false
status: CANONICAL_Q2_VALUES_EQUAL
```

`stale=149` is retained as a separate Core observer-time diagnostic. It does
not count as a source-value mismatch and is not silently discarded from the
report.

## Full-stream replay and anchor status

The same run continued through all input frames with one Engine per pass and a
second identical ordered pass:

```text
frames: 500 (422 non-empty, 78 empty)
updates: 1,209,672
symbols: 5,222
processed signals: 507 per pass
reducer revision: 503 per pass
VirtualClock: 09:39:59 +08:00
ordered/repeat final state hash:
  7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d
deterministic: true
```

The 0920/0924/0925 T1 sidecars were applied to the in-memory replay as in the
existing runner. Their standalone anchor facts remain partial. Do not confuse
`fact_status_counts` (adjacent-anchor comparisons) with
`auction_anchor_fact_status_counts` (the anchor's own observation):

| Anchor | Adjacent comparison | Standalone anchor facts |
|---|---|---|
| 0920 | `PENDING` 5,222 | `AVAILABLE` 1,319; `MISSING` 3,903 |
| 0924 | `PENDING` 5,222 | `AVAILABLE` 3,422; `MISSING` 1,800 |
| 0925 | `PARTIAL` 5,222 | `AVAILABLE` 5,068; `MISSING` 154 |

The 09:32 opening calculation remains `READY=5,210`, `PARTIAL=12` in the
observed 5,222-symbol cohort. This is not proof of full-market coverage.

## Verification

The current implementation passed:

```text
targeted passive-checkpoint tests: 2 passed
full pytest suite: 834 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
```

Real-run report:
`/home/exedev/validation/task008-0926-core-checkpoint-20261008/core-through-input-with-0926-checkpoint.json`

```text
report SHA-256: 5b284826ea2f92277b50b7d83b1472e46a78940ed2a4f1c7a9693f8709fcef23
```

## Limits and alignment

- T1 sidecar equality here demonstrates the frozen producer-output → Core
  reducer state comparison at 09:26 for this date and these artifacts. It does
  not prove that the producer artifact contains every original TD tick; the
  missing per-slice acquisition manifest is still an evidence gap.
- No new live TD/Redis/Rabbit reads were made in this Core run. There was no
  Redis/TD write, Rabbit consume/ACK, service restart, or production-directory
  modification.
- Rabbit arrival/delivery order and historical `available_at` remain
  `UNKNOWN`; deterministic event-time replay does not establish them.
- Same-date producer snapshots for 09:32/09:40 were not part of this
  comparison. The frozen full-replay package contains only `0920`, `0924`,
  `0925`, and `0926` barrier tags. A read-only scan of the 25 validation files
  named like barrier JSON/JSONL found no exact `barrier_tag=0932` or
  `barrier_tag=0940` record. Existing 2026-09-23 Core replay reports are Core
  outputs, not T1 producer snapshots. Do not substitute current/latest values
  and call them historical cutoff snapshots.
- Q2Frame universe coverage, opening status, and missing auction facts remain
  partial as stated above. This result does not close TASK-008 or unblock M3-1.
- The repository remains dirty with unrelated in-progress changes. No changes
  were committed, pushed, merged, or deployed.

The same-date 09:32/09:40 producer-sidecar search is complete for the retained
barrier artifacts and returned no matching records. Keep those comparisons
`UNAVAILABLE_IN_RETAINED_EVIDENCE`; do not spend another loop re-searching the
same files or synthesize snapshots. Continue only with a concrete Core feature
that is supported by the existing producer inputs, while leaving TASK-008
partial.
