# TASK-008 — same-date auction anchor timing audit

Date: 2026-10-08 (Asia/Shanghai)

## Result

This audit compares real, sealed 2026-09-30 TD auction rows with the same-date
t1-v2 Q2Frame event-time replay. It supports whole-second, feature-scoped
anchor analysis; it does not impose exact subsecond equality or establish
Rabbit arrival / historical visibility.

```text
0920_TD_VS_Q2_CANDIDATE=OBSERVED_DIFFERENCES_NOT_A_GATE
0924_TD_VS_Q2_CANDIDATE=OBSERVED_DIFFERENCES_NOT_A_GATE
0925_POSITIVE_PRICE_CANDIDATES=5030/5030_MATCH
0925_POST_SOFT_ANCHOR_REVISION=UNCHANGED
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Pinned inputs and outputs

- Q2Frame: `/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/deployed_release_q2frame_to_0932.jsonl`
  - SHA-256: `1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`
- TD auction rows: `/home/exedev/validation/task008-same-date-production-assembly-20260930-20261002T054020+0800/td_auction_snapshot_rows.jsonl`
  - SHA-256: `162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d`
- Whole-second TD/Q2 candidate comparison:
  `/home/exedev/validation/task008-anchor-candidate-alignment-20260930-20261008T060542+0800/anchor_candidate_alignment.json`
  - SHA-256: `50b21521c5af1a2dde2f6786997f288af0112a661875b25eff41a2c583d2b5ba`
- 09:25 revision-scope replay:
  `/home/exedev/validation/task008-anchor-revision-scope-20260930-20261008T060542+0800/auction_revision_scope_20260930.json`
  - SHA-256: `53435c14237175bb8618ce9d3d02e0899768e715cd0d70102d527963538685a4`

## Observations

The comparison truncates TD and Q2 source timestamps to Shanghai whole seconds.
Its candidate windows (09:20:00–09:20:20, 09:24:00–09:24:20,
09:25:00–09:25:20, inclusive) follow the inspected t1-v2 source contract; the
artifact-producing binary is not attested. Q2Frame artifact order is a
deterministic replay order, not historical Rabbit delivery order.

| Anchor | TD non-null prices | Whole-second as-of Q2 matches / mismatches | Window-end matches / mismatches | Notes |
|---|---:|---:|---:|---|
| 09:20 | 936 | 925 / 11 | 758 / 178 | All 936 TD-positive prices occur in the Q2 candidate window; candidate values continue changing. |
| 09:24 | 3,229 | 3,163 / 66 | 2,480 / 749 | All 3,229 TD-positive prices occur in the Q2 candidate window; candidate values continue changing. |
| 09:25 | 5,030 | 5,030 / 0 | 5,030 / 0 | 190 TD prices are NULL; no positive Q2 candidate is reported for those rows at the compared cut. |

The current `AuctionTimeline` replay observed the 09:25 anchor at 09:25:06
after consuming through source frame 09:25:02. It produced 5,030 available
and 190 missing anchor prices over the 5,220-symbol observed cohort. The first
frame at or after the 09:25:30 soft deadline was at 09:26:00, 30 seconds after
the deadline. Between those observations, the full Q2 row-state hash changed,
but the anchor observation hash/content hash stayed the same; the timeline
kept revision 1 and retained the same 5,030/190 status counts. This is
consistent with not revising an anchor merely because unrelated Q2 state
advanced. It does not show a late `a25` correction in this artifact.

The 09:25:06 source/frame-time gap (latest included source time 09:25:02) is
recorded as context, not a failure condition. This matches the intended use of
whole-second business time and soft continuation; a few seconds of stored
source-time difference alone is not grounds to reject the replay.

## Limits and alignment

- The Q2Frame is a t1-v2 event-time replay artifact. Rabbit arrival order and
  historical Redis `available_at` are `UNKNOWN`; do not claim that these
  values were live-visible at the historical evaluation instant.
- 09:20/09:24 differences are diagnostic and do not justify new hard timing,
  completeness, or all-symbol equality gates.
- 09:25 price agreement applies only to the 5,030 positive captured TD rows;
  the 190 NULL rows remain missing, and no full-market coverage is implied.
- No code, service, Redis, TD, Rabbit, or production state was modified. The
  audit read sealed local files only. No code changed in this step; targeted
  timeline/alignment tests passed (33 passed), and the previously recorded
  full suite remains 854 passed, compileall PASS, and diff-check PASS.

Keep `TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN`. Continue Core work only
where the pinned input supports the specific feature; do not wait on exact
second-level parity for unrelated development.

## Follow-up — direct Core anchors against same-date TD rows (2026-10-08)

The latest current-Core two-pass report was compared symbol-by-symbol against
all three same-date `auction_snapshot_v2` tags. This supplements the earlier
Q2-candidate-window audit with the actual Core `AuctionAnchorFact` result:

| Tag | Common symbols | Core available | TD positive | Equal values | Both available but unequal | Core available / TD NULL | Missing matches |
|---|---:|---:|---:|---:|---:|---:|---:|
| 09:20 | 5,210 | 1,197 | 936 | 925 | 11 | 260 | 4,014 |
| 09:24 | 5,215 | 3,309 | 3,229 | 3,163 | 66 | 80 | 1,906 |
| 09:25 | 5,220 | 5,030 | 5,030 | 5,030 | 0 | 0 | 190 |

There are additionally 10 Core-only symbols at 09:20 (one available, nine
unknown) and five Core-only unknown symbols at 09:24. These are cohort
differences in the retained artifacts, not automatically source loss. The
09:20/09:24 value and availability differences are diagnostic observations;
their cause is not established, and they do not gate independent 09:25
analysis. In particular, do not infer Rabbit arrival or late delivery from
these comparisons.

The 09:25 Core evaluation was at 09:25:06 after source frames through
09:25:02 (4,000 ms). For the 5,030 available anchors, Core `source_time_ms`
preceded the TD row `ts` by 4,026–6,026 ms (median absolute difference 6,026
ms). The timestamp fields' clock semantics are not proven equivalent; this
is not arrival latency and does not affect parity. The few-second difference
is descriptive, not a rejection condition.

Runner: `examples/audit_task008_core_anchor_td_snapshot.py`; regression tests:
`tests/test_task008_core_anchor_td_snapshot.py` (5 passed). Latest all-tag
artifact: `/home/exedev/validation/task008-core-anchor-td-snapshot-all-tags-20260930-20261008T064902+0800/`.
It pins the current two-pass Core report (`3d1000b5…`), TD capture
(`162c259b…`), and t1-v2 Q2Frame input (`1f712b20…`); output checksums pass.
Full Core suite: 869 passed; compileall and diff-check pass. This remains
feature-scoped evidence: Rabbit arrival, historical `available_at`,
full-market coverage, and NORMAL acceptance are unproven. No live service/source
access or production side effect occurred.
