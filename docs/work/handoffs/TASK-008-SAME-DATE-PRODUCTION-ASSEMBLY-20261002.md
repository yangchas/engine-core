# TASK-008 — same-date production assembly and Core partial facts

Audit date: 2026-10-02 (Asia/Shanghai)

## Result

```text
SAME_DATE_TD_REPLAY_INPUT=PASS_WITH_LIMITS
ACTIVE_RELEASE_ASSEMBLY=PARTIAL
CORE_PARTIAL_PLATE_FACTS=PASS_WITH_LIMITS
CORE_REPEAT_HASH=PASS
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
TASK-008=PARTIAL_EVIDENCE
```

This closes a real-data calculation and assembly comparison, not production
acceptance. It does not establish Rabbit arrival order, historical
`available_at`, full-market coverage, or that the production report actually
suppressed plate output on 2026-09-30.

## 2026-09-30 exact active-release assembly

The active `engine-next` release was read-only inspected at
`/home/exedev/services/engine-next/releases/20260903_e272842`. Source hashes:

- `production_fact_assembly.py`: `3b145d6225031aacedf3a83f79bff139f51854b45809e469600cf6581180a2c8`
- `auction_shadow.py`: `a3a99afa64772fc5ab49072318375574007f2f34faae56cac903b45f1d7ebea3`
- `auction_email_report.py`: `b33b173cad109e793bc9aa949cbe5c31a0ef3393876b8b89ec01af01c3807a33`

The pure release assembler consumed the same-date frozen Redis summary/anchor,
the same-date runtime mapping snapshot, and actual TD SELECT rows for 0920,
0924, and 0925. Historical Redis keys had expired; the audit used sealed
same-date captures rather than treating today's Redis contents as history.
No release report sender or effect path was called.

Observed release result:

- report/bundle status: `PARTIAL`;
- market overview: `AVAILABLE`;
- plate facts: `UNAVAILABLE` due to effective auction-universe mismatch;
- frozen Redis anchor: 5,210 symbols; TD 0925: 5,220; shared: 5,210;
- 10 TD-only symbols have zero amount/bid/ask but null `chg_bp`; release
  classification is `UNKNOWN`, not proven inactive.
- Fresh TD 0925 SELECT exactly matched the sealed TD capture over 5,220
  symbols and the compared timestamp/price/change/amount/resting-book fields.

This is a reproduced pure-assembly result, not a captured user-facing report.
The 10 unknown rows must remain visible; no equality or second-level timestamp
gate is added.

## Core partial aggregation on actual same-date inputs

Core consumed the frozen same-date mapping (5,964 symbols) and the 15,645
captured actual TD rows (0920: 5,210; 0924: 5,215; 0925: 5,220). The exact
per-symbol 0924→0925 delta records matched the active release pure formula:
5,220 records, zero mismatches. Five real 0925 symbols lacked a 0924 row and
remain explicitly `unavailable` in Core.

Core aggregated the facts without a global all-or-nothing universe gate:

- 450 mapped plates: 149 `available`, 229 `partial`, 72 `unavailable`;
- pressure facts: 3,093 usable, 2,058 unavailable, 813 missing mapping-member
  observations, zero invalid;
- coverage median 0.667 (range 0–1); full-market coverage remains `UNPROVEN`;
- decision remains `FACT_ONLY`;
- two identical runs produced content hash
  `bac748437bb092e04b6fb7deaa7a5b12aa1dc55bdbbb26526db9c8b91469b67e`.

Evidence directory:
`/home/exedev/validation/task008-core-partial-pressure-20260930-20261002T054243+0800/`.
Its checksum manifest passed. The row input is pinned at
`/home/exedev/validation/task008-same-date-production-assembly-20260930-20261002T054020+0800/`.

## Existing same-date full opening integration (2026-09-29)

The separate full Core opening replay used a real t1-v2 Q2Frame through
09:32:10 plus same-date captured TD auction rows and a frozen plate mapping.
The actual TD-derived pressure context was carried into
`OPENING_0932.plate_auction_pressure_summary`; it remained `FACT_ONLY` and
`UNPROVEN` for full-market coverage. Its derivation matched the pure release
per-symbol pressure formula on all 10 selected plates (zero mismatches), and
the two Q2Frame engine runs had identical final state hashes. All artifact
checksums passed under
`/home/exedev/validation/task008-opening-pressure-integrated-20261002T010630+0800/`.

This demonstrates that the existing Core replay can consume real Q2Frame data
and a real, same-date auction sidecar in one opening run. It still does not
prove the sidecar was available to the historical live process at that time;
producer binary attestation and historical availability remain unknown.

## Additional check and known boundary

Targeted regression tests for opening facts, anchor deltas, and the real
auction-pressure parity/gate audits passed: `69 passed`; `git diff --check`
passed. The repository worktree already contains unrelated user changes; no
Core source was edited in this continuation.

A counterfactual direct call to the release's private plate helper with the
unmatched 0925-only rows raises an `AttributeError` when the prior 0924 row is
absent. The real 09-30 assembler's universe gate prevents reaching that helper,
so this is a latent edge in a bypass path, not an observed production outage.
Do not modify the deployed release as part of TASK-008.

## Safety and status

TD access was SELECT-only. Redis was not accessed during the Core partial
aggregation; sealed captures were used. No Rabbit, ACK, Redis/TD writes,
service action, deployment, restart, notification, or other effect occurred.

```text
TASK-008=PARTIAL_EVIDENCE
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

Next useful evidence is a same-date full t1-v2 Q2Frame through the opening
window plus same-date frozen auction/Q2 evidence, then a Core replay using both
inputs. Do not combine different trade dates or treat a historical TD query as
proof of historical availability. Do not auto-promote the next task.
