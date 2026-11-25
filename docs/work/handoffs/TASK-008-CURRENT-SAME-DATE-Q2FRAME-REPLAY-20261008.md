# TASK-008 current-source same-date Q2Frame replay

Date: 2026-10-08 (Asia/Shanghai)

## Outcome

Current Core source replayed the real, hash-pinned 2026-09-30 t1-v2 Q2Frame
through the 09:32:10 opening evaluation. Ordered and repeat passes used one
Engine and matched the 2026-10-02 archived run on all comparable output fields:
input hash, deterministic status, frame/update/signal/revision/clock counts,
final-state hash, auction-anchor status counts, opening fact hashes/statuses,
and pressure-context hash.

```text
CURRENT_CORE_SAME_DATE_REPLAY=PASS_DETERMINISTIC_WITH_LIMITS
TASK-008=PARTIAL_EVIDENCE
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

## Run and evidence

- Validation directory:
  `/home/exedev/validation/task008-0930-current-core-replay-20261008T015810+0800/`
- Detailed audit: `replay_audit.md`; checksums: `sha256sums.txt`.
- Q2Frame SHA-256:
  `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`.
- Pressure-context file SHA-256:
  `a6e483750a7b10da15e8b3cc55c108c46c51ce2505051459dc01d0303f6e56d5`.
- Current report SHA-256:
  `d0f7adbe5ba46b7aca3b26535ff2ec39461f97312f4547b42f49d58974c2cfe7`.
- Current source was HEAD `a18654e10af663c48dc0571c5baa6c457baebaf0` plus
  the pre-existing dirty worktree and the clock-string validation repair; no
  commit was created.
- The full suite passes: 837 passed; compileall and diff-check pass.

The 758-frame artifact contains 434,188 updates and 5,220 observed symbols.
Core processed 754 frames / 428,586 updates through 09:32:10; the first
excluded frame is 09:32:11. Opening facts are 5,213 READY and 7 PARTIAL
(seven stale under the explicit 60-second freshness policy, zero missing).
The observed cohort comes from the Q2Frame itself; full-market coverage is
unproven. 09:20/09:24 adjacent-anchor comparisons are PENDING. At 09:25,
those adjacent-comparison facts are 5,211 PARTIAL / 9 MISSING, while the
separate standalone auction-anchor availability is 5,030 AVAILABLE / 190
MISSING. These are different contracts and must not be conflated. This run did
not provide a barrier Q2Frame sidecar, so PENDING does not prove either data
absence at the producer or an implementation defect.

Across the entire artifact, 7,537 updates have source time different from
their containing frame time; 7,527 are at least 60 seconds old, with a maximum
of 900 seconds. These are source-time ages, not Rabbit residence or arrival
latency. They are diagnostics, not a hard replay gate. The 09:25:06 snapshot
with a latest source value four seconds earlier is retained as observed and is
not classified as failure.

This Core run read local frozen artifacts only. It did not connect to Redis,
TDengine, or RabbitMQ; it performed no production writes, ACKs, service
controls, or effects. Rabbit delivery/arrival, historical `available_at`,
producer build attestation, full-market coverage, and NORMAL opening
acceptance remain unproven. The adjacent auction-command JSONL contains Redis
command records, not the runner's barrier-snapshot input format.

## Independent 09:25 → 09:32 source/formula verification

The transition output was checked against hash-pinned real inputs rather than
synthetic fixture rows:

- 2026-09-30 t1-v2 Q2Frame SHA-256:
  `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`.
- 2026-09-30 TD auction snapshot SHA-256:
  `162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d`.
- Current Core report SHA-256:
  `d0f7adbe5ba46b7aca3b26535ff2ec39461f97312f4547b42f49d58974c2cfe7`.

At the Core report's 09:32:10 cutoff, an independent file reader selected the
latest per-symbol Q2Frame update in replay order (754 frames / 428,586 updates /
5,220 symbols), then recomputed the opening, auction, and transition
percentages from integer price fields. Results:

```text
Core opening change_pct vs raw Q2Frame calculation: 5,220 / 5,220 equal
transition opening_change_pct vs raw Q2Frame calculation: 5,220 / 5,220 equal
transition source timestamp vs selected Q2Frame update: 5,220 / 5,220 equal
Core 09:25 anchor vs same-date TD positive prices: 5,030 / 5,030 equal
Core missing anchors vs same-date TD NULL prices: 190 / 190 equal
auction-change formula matches: 5,030 / 5,030
opening-minus-auction delta formula matches: 5,030 / 5,030
value/formula mismatches: 0
```

The 5,030 comparable rows use same-date TD 09:25 prices and the selected real
09:32 Q2Frame prices/pre-close values. Seven opening symbols are stale under
the existing 60-second freshness policy (maximum observed source age is
1,030,000 ms); Core keeps them visible as PARTIAL rather than stopping the
cohort. Rabbit arrival and historical `available_at` are not represented by
these event-time files.

A separate same-date producer-output comparison was also made for 2026-09-29,
without mixing it into the 2026-09-30 result. The exact archived t1-v2
replay 09:32 cutoff payload (not a live NORMAL observation) has 5,211 rows.
Against the current Core replay's
transition output, all 5,211 opening percentages recomputed from producer
`px_milli/pc_milli` match; source timestamps match for 5,207 rows and differ
by -3 seconds for four. Delta arithmetic matches for all 5,070 symbols with an
available Core 09:25 anchor; 141 published rows have no such anchor in that
Core cohort. Inputs: 2026-09-29 Q2Frame SHA-256
`5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`, auction
command capture SHA-256
`3422c51fe911172cef247a0eb23ad98f7987a34325abc83db943bebf76f5993f`, producer
payload SHA-256
`97c95133a15e1bb6f1c1f125fe65dacdd1b4f16a71a6a524410c027f3f5af04d`, and
current Core report SHA-256
`4474dc28d8a3ff24890963a0cd4481970d65cfe4431be4237d72885624f133de`. The four
timestamp differences are recorded as observations, not a rejection gate.

This is a feature-level calculation and producer-payload comparison, not
proof of historical Redis visibility, Rabbit order, full-market coverage, or
NORMAL acceptance. No live source or service was queried and no production
side effect occurred. Targeted current tests: 78 passed; compileall and
`git diff --check` passed.

## Independent real-Q2 breadth recomputation (2026-10-08)

The 09:32:10 `CrossSectionFactsV1.market_breadth` was independently recomputed
from the exact Q2Frame rows consumed by the current Core report, using the
latest per-symbol `px/pc` values in the first 754 frames (through logical time
09:32:10). The source frame/update counts, 5,220-symbol cohort, per-symbol
source timestamps, and opening percentage formula were also checked against
the report:

```text
Q2Frame source SHA-256: 1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a
Core report SHA-256:    3d1000b522d960bd0081fbd657d907ffbda43dbb5db7ac6b3ae84e366359daa2
frames / updates:       754 / 428,586
observed symbols:       5,220
price/pre-close valid:  5,220 / 5,220
up / down / flat:       3,555 / 1,285 / 380
unknown:                0
stale (>60s):           7
opening formula / source-time mismatches: 0 / 0
```

The independent breadth exactly matches Core's report. It describes the
observed Q2Frame cohort, not a full-market universe. The seven stale
observations remain included in observed-cohort breadth and are separately
surfaced, so this verifies the current calculation contract—not that every
counted quote was fresh at the 09:32:10 wall-clock instant. Rabbit delivery,
historical Redis `available_at`, and full-market coverage remain unproven.
This check read sealed local files only and caused no production side effect.

## Alignment and next useful evidence

This result strengthens same-date replay-development evidence and does not
close TASK-008. Do not add a hard seconds-level equality rule. If barrier
parity is needed, use an actual captured barrier Q2Frame or contemporaneous
producer snapshot; do not reconstruct it from post-run latest Redis state.
Continue feature-scoped development against the verified cohort, preserving
PARTIAL/UNKNOWN values. Do not start M3-1 or promote NORMAL acceptance from
this replay.
