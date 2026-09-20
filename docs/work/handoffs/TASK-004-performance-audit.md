# TASK-004 performance and ECC audit handoff

- task: `TASK-004`
- branch: `codex/task-cross-sectional-performance`
- scope: 500-frame global 3-second cross-sectional replay performance closure
- production status: `M3_1_NORMAL=BLOCKED`, `TD_WRITE_HEALTH=UNPROVEN`
- side effects: `NONE_OBSERVED`

## Evidence

- ordered FRAME: 500 frames, 1,224,811 events, about 458.8 seconds;
  `PASS_WITH_WARN` (5–10 minute band)
- ordered FINAL: about 453.0 seconds; final FULL cross-section parity `PASS`
- both FRAME: ordered about 454.4 seconds, shuffled about 456.4 seconds;
  deterministic comparison `PASS`
- shuffle scope: within-frame event order only; not Rabbit arrival-order
  independence
- source sequence, Rabbit arrival order, and historical `available_at` remain
  `UNKNOWN`
- 98 empty frames and 402 partial frames were retained in the 500-frame
  timeline; no missing value was converted to zero

## ECC production-audit result

- release surface checked: branch state, diff, project-local Codex/ECC config,
  replay runner, tests, and side-effect declarations
- blocking findings: none
- non-blocking finding: full passes remain in `PASS_WITH_WARN`, not a clean
  <=5-minute performance PASS; FRAME mode does not claim per-frame FULL parity
- merge recommendation: integrator review required before TASK-005; do not
  treat replay evidence as NORMAL production evidence

## Verification

- `628 passed`
- `compileall`: PASS
- `git diff --check`: PASS
- ECC plugin `ecc@ecc` version `2.2.1`: installed and enabled
