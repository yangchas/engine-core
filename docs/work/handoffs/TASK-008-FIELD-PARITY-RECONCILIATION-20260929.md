# TASK-008 field-parity evidence reconciliation — 2026-09-29

## Conclusion

The 2026-09-18 amount/rest mismatch and 2026-09-23/24 field matches are not
contradictory results: they use different trade dates, replay windows, and
historical snapshot cohorts. The 2026-09-18 mismatch is reproduced by an
independent candidate-base Q2Frame artifact, but the artifact has no embedded
binary manifest tying it to the executable that wrote the historical TD
snapshot. The cause therefore remains `UNKNOWN`; do not change the calculator
or declare strict snapshot parity from these comparisons.

## Minimal real-data check

At 2026-09-29 11:03 +08:00, the final per-symbol state was reconstructed from
the existing candidate-base real-TD replay artifact:

```text
input: /home/exedev/validation/task008-0925-release-20260909-replay-20260928T003500+0800/q2frame.jsonl
input SHA-256: 3e57bcf162e97d838e55b467d836bc73aefc35e56122ef7ad11d8cfffea66e4b
frames: 605; final logical event time: 2026-09-18 09:25:04 +08:00
Q2 symbols: 5,221
```

One read-only `SELECT` fetched the 5,221 rows from
`market_data1.auction_snapshot_v2` for `trade_date='20260918'` and
`auction_tag='0925'`. Comparing non-NULL snapshot fields with the replay's
final Q2 state produced:

| Field | Comparable/equal | Result |
|---|---:|---|
| `a25` / snapshot price | 4,408 / 4,408 | equal |
| derived change / `chg_bp` | 4,408 / 4,408 | equal |
| `am` / match amount | 3,645 / 5,221 | 1,576 differ |
| `br` / rest bid | 3,595 / 5,221 | 1,626 differ |
| `ar` / rest ask | 3,600 / 5,221 | 1,621 differ |
| `ls` / limit state | 5,220 / 5,221 | one differs |

All comparable fields matched together for 3,588 symbols. There were no
snapshot symbols missing from the replay state. These amount/rest counts
reproduce the existing full-window 09:25:04 comparison; using the candidate
base replay did not eliminate the discrepancy.

## Reconciliation with existing evidence

- Phase F is a 2026-09-18 `[09:24:00,09:25:09)` isolated-Redis replay and
  records the same 1,576 amount, 1,626 rest-bid, and 1,621 rest-ask
  mismatches. Its price/change comparisons match. Its replay Q2 fields are
  internally consistent with the current `AuctionCalculator` on the compared
  TD-derived candidate states.
- Phase H's read-only source correspondence check found 5,184/5,221 snapshot
  amount/rest tuples matching some prior TD event-time state, while
  3,576/5,221 match the latest event-time state before the barrier. That is
  evidence of a mixed per-symbol historical cohort, not proof of Rabbit
  arrival or historical availability.
- Phase K is a 2026-09-23 full 09:15-start replay: 5,208 comparable A2
  symbols match for change, amount, rest bid, and rest ask; 5,068 non-NULL
  prices match, while 140 NULL prices remain unavailable. Phase M reports
  comparable same-date amount/rest-bid matches for 2026-09-24. Neither result
  can be projected backward to 2026-09-18.
- The preserved 2026-09-09 release binary has SHA-256
  `0698b4172b58248bb1eaf4b3efa6d18a8c1fac78334a250455fdff9a3ed84563`; the
  2026-09-23 active patch release is based on it. No artifact manifest proves
  that this exact binary generated the 2026-09-18 snapshot or the Q2Frame used
  above. Release provenance and historical snapshot-writer identity remain
  separate unknowns.

## Decision and boundaries

```text
09-18 amount/rest snapshot parity: PARTIAL / MISMATCH OBSERVED
09-18 event-time latest vs snapshot cohort: DIFFERENT FOR MANY SYMBOLS
cause (release version vs historical visible/arrival cohort): UNKNOWN
Rabbit arrival order / historical available_at: UNKNOWN
Core calculator defect demonstrated: NO
```

Do not change the Q2/AuctionCalculator formula to force equality with the old
snapshot, and do not add a global gate for unrelated Core features. Functions
using `am`, `br`, or `ar` against this 2026-09-18 snapshot must retain the
field-specific limitation; features supported by independently verified Q2
fields may continue on the existing TASK-008 real replay path.

The comparison used a frozen local Q2Frame and a TD `SELECT` only. It did not
write Redis/TD, access Rabbit, change a service, or touch the production tree.
No source code or existing user-modified planning file was changed by this
reconciliation. Current Core verification: `729 passed`; `compileall` and
`git diff --check` passed. TASK-008 remains `PARTIAL_EVIDENCE`, and this audit
does not change M3-1 or TD-write health status.
