# TASK-008 — same-date anchor Q2/TD field alignment diagnostic

Audit date: 2026-10-08 (Asia/Shanghai)
Trade date: 2026-09-30

## Result

This is a source-time candidate diagnostic over sealed real artifacts. It is
not an exact-time gate, a Rabbit-arrival reconstruction, or a historical Redis
visibility test.

```text
0920_FIELD_ALIGNMENT=OBSERVED_DIFFERENCES_NOT_A_GATE
0924_FIELD_ALIGNMENT=OBSERVED_DIFFERENCES_NOT_A_GATE
0925_FIELD_ALIGNMENT=5220_OF_5220_EQUAL_FOR_COMPARED_FIELDS
TASK-008=PARTIAL_EVIDENCE
NORMAL_OPENING_ACCEPTANCE=UNPROVEN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Inputs and reproducibility

- Q2Frame: `/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2frame.jsonl`
  - SHA-256: `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`
- TD auction rows: `/home/exedev/validation/task008-same-date-production-assembly-20260930-20261002T054020+0800/td_auction_snapshot_rows.jsonl`
  - SHA-256: `162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d`
- Analyzer: `examples/audit_task008_anchor_q2_field_alignment.py`
  - SHA-256: `023af7fc7881d407268d221b6bd8e035e06a5ca8d3a0e5ce57afa787675a93b4`
- Targeted regression tests: `tests/test_task008_anchor_q2_field_alignment.py`
  - SHA-256: `83b74f7b2c8f338a7412d87c04b7c0be5c66b0dc91f39141def57c14d47a77f6`
- Core HEAD during audit: `a18654e10af663c48dc0571c5baa6c457baebaf0`; the worktree
  was already dirty and these files were not committed.
- Initial row-diagnostic output: `/home/exedev/validation/task008-anchor-q2-field-alignment-20260930-20261008T061949+0800/`
- Aggregate-impact output: `/home/exedev/validation/task008-anchor-q2-field-alignment-aggregate-20260930-20261008T062355+0800/`
- Latest source-time-range output: `/home/exedev/validation/task008-anchor-q2-field-alignment-final-20260930-20261008T062642+0800/`
- Latest summary SHA-256: `340b33e56955c2a4db1098bce35dab1df57b9eef0c8554bd106810a9996cba20`
- Per-symbol evidence SHA-256: `f96cf37e0e0bff78060948aeea400f78559a1f21614d6e7cab98683b7dca67f9`

The analyzer streamed 603 Q2Frame records / 199,621 updates and read 15,645
TD rows (5,210 at 09:20, 5,215 at 09:24, and 5,220 at 09:25). There were no
malformed frames/updates, invalid symbols/timestamps, wrong-date rows, or
duplicate TD `(tag, symbol)` rows. Each compared TD row had a source-time
candidate; the compared values were non-null and valid in both artifacts.

## Method and limits

For each observed TD `(auction_tag, symbol)`, the analyzer truncates the TD
row's and Q2 update's source timestamps to Shanghai whole seconds. It selects
the greatest Q2 source second no later than the TD row's source second, within
the replay's 09:15 onward input. If several Q2 artifact updates share that
second, the later artifact item is selected. It then compares Q2 `am`, `br`,
and `ar` with TD `match_amt_yuan`, `rest_bid_amt_yuan`, and
`rest_ask_amt_yuan`, respectively. This is a deterministic source-time
candidate rule; artifact order is not Rabbit arrival order.

The pinned TD assembly metadata and this run's explicit cutoff summary show
one exact TD source timestamp per tag: 09:20:03.276, 09:24:10.014, and
09:25:06.026 (Asia/Shanghai). Each tag therefore uses one shared TD
event-time cutoff, although the selected latest-prior Q2 source timestamp
varies by symbol. This is an event-time as-of candidate view, not proof of
atomic Redis visibility at that cutoff.

The exact Q2Frame is the same-date replay artifact used by the existing 09:25
three-field parity evidence. A separate earlier scratch run used a broader
Q2Frame extending later into the session; it is intentionally not the result
reported here. No live source was queried by this follow-up: it analyzed the
pinned sealed files only.

## Observed comparison

| Anchor | TD rows | `am` equal / different (max abs diff, yuan) | `br` equal / different (max abs diff, yuan) | `ar` equal / different (max abs diff, yuan) | Source-time gap rows: 0 / 1–3 / 4–6 / >6 sec |
|---|---:|---:|---:|---:|---:|
| 09:20 | 5,210 | 4,979 / 231 (1,235,455) | 5,013 / 197 (846,630) | 5,055 / 155 (1,558,305) | 268 / 928 / 821 / 3,193 |
| 09:24 | 5,215 | 4,946 / 269 (1,173,408) | 4,969 / 246 (2,975,778) | 5,007 / 208 (3,094,882) | 296 / 654 / 252 / 4,013 |
| 09:25 | 5,220 | 5,220 / 0 (0) | 5,220 / 0 (0) | 5,220 / 0 (0) | 0 / 0 / 5,030 / 190 |

For both 09:20 and 09:24, all observed field differences fall in rows whose
selected Q2 source-time candidate is 0–3 seconds behind the TD row source
second. Every row with a 4-second-or-larger gap is equal for all three fields
in this comparison. The 09:25 cohort is equal for all three fields across all
5,220 observed TD rows. These counts are descriptive; in particular, they do
not imply that waiting four seconds would reproduce live behavior or that a
shorter gap caused the value differences. The largest observed differences
are material and should not be waved away as rounding noise.

## Aggregate impact on the paired observed cohort

The follow-up adds Q2 and TD sums over the same symbols only when both field
values are present and valid. `net` is `Q2 sum - TD sum`; `gross` is the sum
of per-symbol absolute differences. Percentages use the TD sum as denominator.
This quantifies this analyzer's per-symbol source-time candidate set. The
cutoff is common within each tag, but the selected Q2 source timestamps differ
by symbol; these totals do not reconstruct production Redis visibility or
arrival-order aggregation.

| Anchor / field | Compared rows | TD sum (yuan) | Q2 candidate sum (yuan) | Net (yuan; % of TD) | Gross per-symbol abs diff (% of TD) |
|---|---:|---:|---:|---:|---:|
| 09:20 `am` | 5,210 | 2,086,296,173 | 2,093,644,544 | +7,348,371 (+0.35%) | 8,384,963 (0.40%) |
| 09:20 `br` | 5,210 | 10,486,960,913 | 10,491,482,355 | +4,521,442 (+0.04%) | 11,303,382 (0.11%) |
| 09:20 `ar` | 5,210 | 1,011,352,663 | 1,011,979,418 | +626,755 (+0.06%) | 8,418,049 (0.83%) |
| 09:24 `am` | 5,215 | 5,237,926,865 | 5,256,067,379 | +18,140,514 (+0.35%) | 18,223,448 (0.35%) |
| 09:24 `br` | 5,215 | 11,497,918,245 | 11,504,711,432 | +6,793,187 (+0.06%) | 23,225,535 (0.20%) |
| 09:24 `ar` | 5,215 | 1,303,761,581 | 1,300,359,600 | -3,401,981 (-0.26%) | 19,897,089 (1.53%) |
| 09:25 `am` | 5,220 | 11,881,094,371 | 11,881,094,371 | 0 (0.00%) | 0 (0.00%) |
| 09:25 `br` | 5,220 | 1,522,496,390 | 1,522,496,390 | 0 (0.00%) | 0 (0.00%) |
| 09:25 `ar` | 5,220 | 1,223,251,270 | 1,223,251,270 | 0 (0.00%) | 0 (0.00%) |

The modest net differences at 09:20/09:24 coexist with larger gross per-symbol
differences, so cancellation makes the event-time cohort total look closer
than the cross-section actually is. Conversely, this diagnostic alone does
not show that a strategy or plate aggregate would change by the same amount:
it neither applies plate membership nor reproduces common-time Redis
visibility. The
09:25 `am` sum also equals the previously captured same-date market-summary
amount, consistent with the existing per-symbol parity evidence; it is not a
new independent producer-path test.

## Interpretation and next use

The result supports a narrow hypothesis for future investigation: 09:20/09:24
field differences are associated with selecting a Q2 event-time candidate
close to the TD row's source second, while older candidates in this artifact
match. Plausible explanations include source-time progression or anchor
cohort timing, but causality is not established. The artifacts do not reveal
per-key Redis state at the instant of the TD snapshot, Rabbit delivery/arrival
order, or historical `available_at`; therefore they cannot distinguish those
explanations or certify the selected Q2 candidate as the live-visible value.

Use this only to guide feature-scoped replay analysis. Do not add a strict
seconds gate, global halt, or claim of production parity. Whole-second
truncation deliberately treats fractional seconds as the same second, in
line with the project's tolerance for small storage-time differences.

- `q2_frame_order_semantics=DETERMINISTIC_EVENT_TIME_ARTIFACT_ORDER_NOT_RABBIT_ARRIVAL`
- `historical_available_at=UNKNOWN`
- `live_visibility=UNKNOWN`
- `full_market_coverage=UNPROVEN` (the comparison is limited to captured TD rows)
- `side_effects=NONE_OBSERVED` (sealed files only; no Redis/TD/Rabbit/service writes)

## Code validation

- Targeted analyzer tests: `5 passed`.
- Full Core suite after source-time range and aggregate accounting: `859 passed` (three dependency
  deprecation warnings).
- `compileall -q src tests examples`: passed.
- `git diff --check`: passed.
- The analyzer is file-only; the audit did not connect to Redis, TDengine, or
  RabbitMQ, and did not modify a service or production data.

The evidence does not change task or production gates. Keep
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN`.

## Plate-impact sensitivity diagnostic (same sealed inputs)

The follow-up groups the same per-symbol source-time candidates by the frozen
same-date `stock -> one plate` mapping. This is a sensitivity diagnostic, not
the production plate assembler, not strategy output, and not a new acceptance
gate.

- Mapping snapshot:
  `/home/exedev/services/engine-next/shared/runtime_state/2026-09-30/stock_plate_snapshot.json`
  - SHA-256: `8dbf5a1d689d5d2acba09795133305ed28e3eedc7e8ebf0acc69261ac02fccbc`
  - effective time: `2026-09-30T08:30:32.429671`; this does not prove the mapping
    was the exact mapping visible to the production auction calculation.
- Per-symbol candidate evidence:
  `/home/exedev/validation/task008-anchor-q2-field-alignment-final-20260930-20261008T062642+0800/symbol_field_evidence.jsonl`
  - SHA-256: `f96cf37e0e0bff78060948aeea400f78559a1f21614d6e7cab98683b7dca67f9`
- Analyzer: `examples/audit_task008_anchor_q2_plate_impact.py`
  - SHA-256: `95f8e71bda43eed707083f8e631a6fb7d0a3d47ec2e44372752fcaa02291389f`
- Targeted tests: `tests/test_task008_anchor_q2_plate_impact.py`
  - SHA-256: `2da4a0a91c9089a59f1162b9ff68e077e8899cdce835b7003358167e6c087b81`
- Output:
  `/home/exedev/validation/task008-anchor-q2-plate-impact-final-20260930-20261008T063147-v2/`
  - summary SHA-256: `ae32e2f1384a618a54d4d75dfe794dadd9dac12ea8d6d437e03bc08c806cdabf`
  - per-plate rows SHA-256: `37137a4262b133e34b4903073511e17774b36c6cc70f20a16d2876240ba71fbf`
  - both entries in `sha256sums.txt` revalidated successfully.

The frozen map has 5,964 symbols and 450 plate labels. Of the captured TD
cohorts, 5,141/5,210 (09:20), 5,146/5,215 (09:24), and 5,151/5,220 (09:25)
were mapped; each anchor has 69 unmapped TD symbols. Conversely, 823/818/813
symbols present in the map were absent from the respective TD capture. The
mapped intersection is about 98.68% of captured TD rows, but this is not
full-market coverage. Every `am/br/ar` per-plate partition reconciles exactly
to its mapped-cohort comparison totals.

Within that mapped intersection, 09:20 had 229/195/153 differing `am/br/ar`
symbols and gross per-symbol absolute differences of 8.38m/11.30m/8.15m yuan.
At 09:24, 265/242/208 symbols differed, with gross differences of
17.78m/22.28m/19.90m yuan. A notable plate-level concentration is 09:24 `ar`
in 房地产 (39 mapped observed members; 3.13m yuan gross difference); 09:24
`br` in 英伟达 has 2.98m gross difference across only four members and should
not be read as a broad-market result. At 09:25, all three fields match across
the 5,151 mapped observed members; top-by-zero entries are not mismatches.

Only `PRESENT/PRESENT` pairs contribute to sums; NULL, invalid, or unmapped
symbols are not turned into zero. The 69 unmapped symbols remain outside the
plate aggregates, not silently assigned. This analysis uses the sealed
event-time Q2 candidate and same-date frozen mapping; Rabbit arrival order,
historical Redis visibility, `available_at`, exact production mapping
visibility, and full-market coverage remain UNKNOWN/UNPROVEN. No Redis,
TDengine, RabbitMQ, or production service was accessed in this follow-up;
side effects are `NONE_OBSERVED`.

Five focused tests cover mapped/unmapped members, NULL versus zero,
trade-date mismatch, duplicate `(anchor,symbol)` rejection, and unsupported
field-status rejection. The unsupported-status check was added after review
found that malformed non-PRESENT statuses were not rejected; the pinned real
input re-run produced identical summary and per-plate hashes. Targeted tests:
`5 passed`; full Core suite: `864 passed` with three dependency deprecation
warnings; `compileall` and `git diff --check` passed. The run status remains
`OBSERVED_DIAGNOSTIC_NOT_A_GATE`; no timing/coverage hard gate was added, and
all task and production statuses above remain unchanged.

## Active engine-next plate-path contract cross-check

The per-plate candidate table above is **not** an exact comparison against an
engine-next production plate output. Read-only service metadata shows the
running process working directory is
`/home/exedev/services/engine-next/releases/20260903_e272842`; the `current`
symlink resolves to the same release. The inspected active-release source
hashes are:

- `engine_next/strategy_skill_layer/auction_plate_buckets.py`:
  `c530b41b10643204eb89ce3b73b720e01caade4936643aa215948f686ab90af0`
- `engine_next/runtime/intraday_context_builder.py`:
  `255412981882ee9f916f273c011e8370c1014ab39a6aa7bc1252613944b55654`
- `engine_next/runtime/intraday_data_hub.py`:
  `645ab676d921c6437b28efce3535b4ee32cac8483334610c5dc30e532f634c5b`

Those sources establish materially different contracts:

1. `build_auction_plate_bucket_stats` consumes a full `IntradayContext`, not
   just `am/br/ar` plus one primary plate. It resolves up to two plate names
   from `snapshot.plate` and `real_plate_names`, gives them weights 1.0/0.6
   (with a 0.18 multiplier for generic plates), excludes a snapshot when
   `abs(current_pct) > 0.35`, and only adds positive `auction_amount` to the
   auction amount sum. Its returned rows are also ranked/truncated by `top_n`
   (default 5), with score and other market/context facts. The diagnostic above
   instead emits unweighted all-450-plate sums over the mapped captured cohort.
2. Q2 normalization exposes `am` as `q2_auction_amount_yuan`, but the
   `StockStateSnapshot.auction_amount` builder prefers an auction-row
   `amount` when that row exists and otherwise falls back to the Q2 amount.
   The builder also needs contemporaneous quote/cache, auction, plate/reason,
   yesterday-limit, and hot-plate context to reproduce production snapshots.
3. The frozen `RuntimeStockPlateSnapshotV1` used above has source
   `market:stock_plate` and only one primary plate per symbol. The searched
   2026-09-30 validation artifacts do not contain a complete same-time
   `IntradayContext` / `real_plate_names` payload. Therefore multi-plate
   weighting, production `auction_amount` overrides, and the exact
   comparable-snapshot cohort cannot be reconstructed from this Q2Frame and
   map alone. `NOT_COMPARABLE_WITH_PRODUCTION_AUCTION_BUCKET_OUTPUT`.

The retained 2026-09-30 read-only context evidence is insufficient to fill
that gap: the `engine_next_context_probe_20260930T064014+0800.json` artifact
contains three selected symbols only (snapshot_count=3) and reports no latest
quote timestamp; it is a premarket probe, not the auction-time universe. The
`context_input_key_inventory_20260930T070725+0800.json` records the date-scoped
Q2/auction/hot-plate/yesterday-pool keys as absent at that premarket
observation, while undated global hashes had counts 5,964 (`market:stock_plate`),
2,983 (`config:plate_mapping:s2p`), and 2,559 (`market:stock_reason`) with
historical version explicitly `UNKNOWN`. These counts do not provide the
per-symbol values or prove which mapping/reason data was used at 09:20/09:24/
09:25. Evidence lives under
`/home/exedev/validation/task008-opening-breadth-20260930/`.

Separately, the sealed exact-release pure fact-assembly audit already exists
at `/home/exedev/validation/task008-same-date-production-assembly-20260930-20261002T054020+0800/`;
its manifest was revalidated in this audit. It records the active release
source hashes and a pure assembly result of `report_status=PARTIAL`,
`market_overview=available`, `mapping=available`, `plate_facts=unavailable`.
The reason is the effective-universe mismatch: frozen anchor 5,210 symbols,
TD 5,220, with ten TD-only symbols classified `UNKNOWN`, not proven inactive.
The component degradation is local: market overview remains available. It is
not evidence that auction fields failed or that a few seconds of storage
offset should stop the process.

That same audit also records a direct counterfactual call to the release's
plate delta helper failing on five real 09:25-only members absent from 09:24
(`AttributeError`, missing prior record). This was not the production report
path—the fact assembler withheld plate facts first—and was explicitly marked
`COUNTERFACTUAL_ONLY_NOT_PRODUCTION_OUTPUT`. No production source was changed.
Core's same-date field-delta path has already retained missing anchor pairs as
unavailable rather than fabricating a zero delta; keep this as a migration
regression requirement, not a reason to loosen or copy the legacy failure.

Accordingly, the sensitivity diagnostic remains useful for locating which
primary-map buckets carry observed candidate differences, but it must not be
reported as production plate parity. A true strategic-bucket parity run needs
the same-time `IntradayContext` inputs (especially `real_plate_names` and
auction-row overrides); if those artifacts cannot be recovered, record the
parity item as `UNVERIFIED` and continue Core work with the existing
`FACT_ONLY`/partial-evidence path rather than inventing missing context.

## Whole-window value-occurrence follow-up (2026-10-08)

To avoid treating a few seconds of timestamp alignment as the acceptance
criterion, the pinned TD rows and Q2Frame were independently scanned by symbol
from 09:15 through each anchor's inclusive `+20s` candidate window. The
existing whole-second as-of rule was reproduced first; its mismatch counts
matched the field-alignment summary. Zero and nonzero TD values were counted
separately. This scan is event-time diagnostics only; it cannot establish
when Rabbit delivered an update or when Redis exposed it.

| Anchor / field | TD nonzero values | Seen somewhere in Q2 event history by anchor +20s | As-of value differences (nonzero only) | Differing nonzero values that reappeared after TD row second |
|---|---:|---:|---:|---:|
| 09:20 `am` | 4,761 | 4,761 | 215 | 0 |
| 09:20 `br` | 2,520 | 2,520 | 175 | 13 |
| 09:20 `ar` | 2,012 | 2,012 | 137 | 5 |
| 09:24 `am` | 5,057 | 5,057 | 269 | 0 |
| 09:24 `br` | 2,665 | 2,665 | 222 | 7 |
| 09:24 `ar` | 2,230 | 2,230 | 184 | 3 |
| 09:25 `am` | 5,210 | 5,210 | 0 | 0 |
| 09:25 `br` | 5,122 | 5,122 | 0 | 0 |
| 09:25 `ar` | 5,099 | 5,099 | 0 | 0 |

Every nonzero TD value in the 09:20/09:24 cohorts occurred somewhere in the
retained same-date Q2 event-time history, but the selected latest-prior
candidate still differs for a subset. Only a small portion of those differing
values reappeared after the TD row's whole-second timestamp within the
candidate window. This shows that exact latest-prior event-time lookup is not
a sufficient reconstruction of the stored early-anchor snapshot; it does not
identify the cause or prove live visibility. At 09:25, all compared amount and
resting-book values agree, including zero-valued rows. Do not infer that
09:20/09:24 should wait a fixed number of seconds, add a second-level gate, or
block independent 09:25 facts.

Inputs revalidated in this follow-up:

- Q2Frame SHA-256: `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`
  (603 frames / 199,621 updates).
- TD auction rows SHA-256: `162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d`
  (5,210 / 5,215 / 5,220 rows for 09:20 / 09:24 / 09:25).

The Q2 artifact's exact producer binary remains unattested. This strengthens
bounded event-time development evidence only; Rabbit arrival, historical
Redis visibility/`available_at`, and production cutoff parity remain UNKNOWN.
No Core behavior or acceptance status changed.
