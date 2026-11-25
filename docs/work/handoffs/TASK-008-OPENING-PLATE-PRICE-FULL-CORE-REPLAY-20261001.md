# TASK-008 — full Core replay with TD-derived opening price context

Date: 2026-10-01 (Asia/Shanghai)

## Result

```text
FULL_Q2FRAME_CORE_RUNNER=PASS_WITH_LIMITS
TWO_RUN_DETERMINISM=PASS
TD_DERIVED_PRICE_CONTEXT_REACHES_OPENING_0932=PASS
TD_Q2_ANCHOR_TIME_ALIGNMENT=OBSERVED_WITH_DIFFERENCES_NOT_A_GATE
OPENING_FACTS=5211_READY_12_PARTIAL
NORMAL_OPENING_ACCEPTANCE=NOT_EVALUATED
TASK-008=PARTIAL_EVIDENCE
```

This closes the wiring/aggregation check proposed in the previous
price-recompute handoff: the independently TD-derived
`OpeningPlatePriceReferenceV1` is supplied to the post-engine
`plate_price_summary` assembly in `OPENING_0932`. It does **not** enter the
Engine's per-symbol input or strategy facts. It does not establish Rabbit
delivery/arrival equivalence or production NORMAL acceptance.

## Pinned inputs and run

- Q2Frame: `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl`
- Q2Frame SHA-256: `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`
- Amount context: `/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/opening_plate_amount_context.json`
- Amount context hash: `aefeada38756163cfd01d57bcb125c27f4e55d2387fce0aa7816fe0b092cd2c0`
- Price reference context: `/home/exedev/validation/task008-opening-plate-price-td-recompute-20261001T211459+0800/opening_plate_price_reference_context.json`
- Price reference hash: `d2d5b18e363a93752dbe4f2dc994876d49db46a3c1584b57468463905072c11b`
- Captured TD rows: `/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/td_auction_snapshot_rows.jsonl`
- Captured TD rows SHA-256: `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`
- TD auction rows were captured historical evidence; this run did not connect to TD, Redis, or Rabbit.

The runner processed the pinned input twice, with one Engine instance per run.
Each pass reached 09:32:10 with 734 frames / 418,759 updates processed, 738
signals, reducer revision 734, and the same final state hash:
`59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27`.
All reported determinism dimensions passed, including opening evidence and
input SHA stability. Inventory contained 735 non-empty frames and 5,223
symbols; one frame after the evaluation cutoff was not processed.

The opening report's price-reference hash equals the pinned context hash, and
its plate-summary hash
`9bc2d45b3181a08887ff995312d1bcd926e59b06d585ca460ae54fc0d945fab8`
matches the independently TD-recomputed summary from the preceding handoff.
This confirms that the new reference context is consumed by the full Core
runner, rather than only by the standalone summary audit.

## TD/Q2 anchor candidate timing reconciliation

A separate read-only diagnostic compared the hash-pinned TD rows with each
symbol's `a20/a24/a25` candidate states in the pinned Q2Frame artifact. It
truncated both timestamps to Shanghai whole seconds, retained Q2Frame artifact
order within a second, and deliberately did not classify differences as a
pass/fail gate. The TD `_ts` values in this capture are exactly:

- 0920: `09:20:03.259`
- 0924: `09:24:10.250`
- 0925: `09:25:06.154`

The Q2Frame contains 735 frames / 419,533 updates; no Q2 update has subsecond
timestamp precision. Its adjacent t1-v2 log identifies this as `mode=replay`,
with Redis/TD writes skipped and no ACK. The source checkout's candidate
windows are inclusive through 09:20:20, 09:24:20, and 09:25:20, respectively;
the binary that emitted this artifact is not attested.

At each TD row's whole-second cut, all TD-positive prices had a positive Q2
candidate available. Exact price matches were 811/820 at 0920 (9 different),
3,019/3,077 at 0924 (58 different), and 4,585/4,585 at 0925. Every TD-positive
price (820/820, 3,077/3,077, 4,585/4,585) also appeared somewhere in its
corresponding Q2 replay candidate history. Q2 had positive candidate values
where TD `px_milli` was NULL for 244/60/485 symbols; the full Core report's
available anchor counts therefore reconcile exactly as 1,064/3,137/5,070.

The candidate continued changing after the 0920/0924 TD row cuts: 381 symbols
had multiple positive a20 values and 838 had multiple positive a24 values in
the replay windows. Comparing against the later window-end candidate instead
would yield 173/690 price differences; that is a different temporal cut, not
evidence of a bad freeze. At 0925 the later candidate happened to remain equal
for every TD-positive row.

This supports, but does not prove, the hypothesis that the small 0920/0924
same-second mismatches reflect different per-symbol candidate selection/order
at the freeze, rather than a broad price-unit or formula mismatch. The TD row
timestamp is not historical `available_at`; Q2Frame replay order is not Rabbit
arrival order, and producer-binary identity remains unverified. Do not impose
exact all-symbol equality or a subsecond gate, and do not silently ignore the
9/58 value differences when validating features that consume those anchors.

The diagnostic was repeated from the same pinned files. Both JSON outputs have
SHA-256 `3152c9aead9931c08e9eb11df1392c2215f574b3d339b36b69404c1ee0e19663`:

- `/home/exedev/validation/task008-anchor-candidate-alignment-20261001T223351+0800/`
- `/home/exedev/validation/task008-anchor-candidate-alignment-repeat-20261001T223434+0800/`

The analyzer is `examples/audit_task008_anchor_candidate_alignment.py`; its
three pure boundary tests are in
`tests/test_task008_anchor_candidate_alignment.py`. It is file-only, reports
observations rather than gates, and does not change Core or production replay
semantics.

## Data quality observations

- At `OPENING_0932`, 5,211 symbols were `READY` and 12 were `PARTIAL`/stale;
  no symbols were missing. Each reported opening field (amount, change,
  limit-state, speed) was available for all 5,223 symbols.
- Snapshot anchor availability (`auction_anchor_fact_status_counts`) was:
  0920: 1,064/5,223; 0924: 3,137/5,223; 0925: 5,070/5,223. These are observed
  cohort facts, not a full-market completeness claim.
- The separate `fact_status_counts=PENDING` at 0920 and 0924 belongs to the
  anchor-delta strategy, which awaits later anchor triggers; it is not the
  standalone anchor availability count. At 0925 that strategy status is
  `PARTIAL` because some anchor values are unavailable.
- Spot check: symbol `000006` has `px_milli=null` in the captured 0920 TD row;
  Core records `ANCHOR_FIELD_UNAVAILABLE`, not zero. This is one example, not
  a complete reconciliation of every missing anchor.
- Inventory reports 2,788 frame/source-time differences of at least 60 seconds
  (maximum 900 seconds). The field is explicitly frame logical time minus
  source time, **not arrival latency**; Rabbit arrival and historical
  `available_at` remain unknown.
- The Q2Frame has no empty frames, so this run does not exercise empty-frame
  handling.

## Real-data check: missing 0920/0924 does not block 0925

The pinned full Core report was also checked against the optional-prior-anchor
contract. In its actual 2026-09-29 observed cohort:

```text
0920: 1,064 AVAILABLE / 4,159 MISSING
0924: 3,137 AVAILABLE / 2,086 MISSING
0925: 5,070 AVAILABLE /   153 MISSING
```

Among symbols with an available 0925 anchor, 4,021 had no 0920 anchor, 1,993
had no 0924 anchor, and 1,837 had neither prior anchor. All 1,837 still had an
`available` `OpeningTransitionSummaryV1` fact at 09:32, which uses the 0925
anchor; the 0920/0924-dependent deltas remain separately unavailable rather
than being inferred. For example, symbol `000006` has a missing 0920 anchor,
available 0924 and 0925 anchors, and an available 09:32 transition fact.

For the 153 unavailable 0925 anchor prices, the same report has
`recovery_required=true`; its `RecoveryPlanV1` requests exactly those 153
symbols and only `auction_anchor_0925_price_milli`. The plan is
`REQUESTED`, while `recovery_execution=NOT_RUN_BY_CORE`; the anchor remains
`PARTIAL`, and available-symbol opening facts continue to be emitted. This
verifies the real-data partial-plus-recovery-plan behavior, not a Wencai call,
provider execution, Redis write, or promotion to `READY`.

This is real replay evidence that per-symbol 0920/0924 gaps do not suppress
independent 0925/opening analysis. It does not simulate an entirely absent
0920/0924 source stream; the pure contract tests cover that case. It also does
not claim full-market completeness or Rabbit/live availability. Evidence is
the pinned `core_q2frame_report.json` above (SHA-256
`89a4dd5e17dcebe77c84c7b66c6308a19e0d9373d6904ef7f5633d9be873872f`).

## Verification and limits

- Full suite after the anchor-candidate audit addition: `802 passed, 3 existing protobuf deprecation warnings`.
- Follow-up targeted recovery/anchor/timeline/Q2Frame replay suite:
  `37 passed`.
- `compileall`: PASS.
- `git diff --check`: PASS.
- Core side effects: `NONE`; this was local captured-file input and in-memory
  computation only. No production source, service, Redis, TD, Rabbit, ACK, or
  effect path was used.
- The old `legacy_open_confirmation.json` remains a `replay_fixture_only`
  artifact with unavailable observation time. Its 100x median-unit divergence
  is an artifact finding, not proof of a production report defect.
- TD completeness, exact producer-binary attestation, Rabbit delivery and
  arrival order, historical availability, and NORMAL opening acceptance remain
  `UNKNOWN`/`UNPROVEN`.

Validation report:
`/home/exedev/validation/task008-opening-plate-price-full-core-replay-20261001T212700+0800/core_q2frame_report.json`

Report SHA-256:
`89a4dd5e17dcebe77c84c7b66c6308a19e0d9373d6904ef7f5633d9be873872f`

M3-1 remains `BLOCKED`; `TD_WRITE_HEALTH` remains `UNPROVEN`. No new task or
production stage is promoted by this result.

## Follow-up: partial recovery continuation

The pinned real replay above has 153 unavailable 0925 anchors and emits a
`REQUESTED` plan for exactly those symbols. A source review found that after an
`APPLIED` recovery cohort left some anchors missing, the analysis bundle
suppressed any follow-up plan solely because the prior attempt was marked
`APPLIED`. It also replaced the original source-layer list with only the
recovery source.

The Core-only repair now:

- retains prior source layers and appends the recovery source once;
- keeps the recovered anchor `PARTIAL` rather than promoting it to `READY`;
- emits a new plan only for the still-missing anchor symbols after a partial
  recovery; a complete anchor cohort needs no further plan even though its
  recovery-derived state remains `PARTIAL`.

The regression test exercises a partial fill of an initial two-symbol missing
set: the next plan contains only the remaining symbol, uses the new revision's
idempotency key, and preserves the original Q2 plus recovery source layers.
The idempotent complete-fill case remains covered. Targeted suite: `30 passed`;
full suite: `803 passed, 3 existing protobuf deprecation warnings`;
`compileall` and `git diff --check`: PASS.

Real-data boundary check: the existing pinned report remains SHA-256
`89a4dd5e17dcebe77c84c7b66c6308a19e0d9373d6904ef7f5633d9be873872f`, with
0925 `5070 AVAILABLE / 153 MISSING`, a `REQUESTED` recovery plan for 153
symbols, and `recovery_execution=NOT_RUN_BY_CORE`. This confirms the input
cohort and first-plan scope; it is not evidence that a provider executed. A
post-change full replay was started but deliberately stopped after confirming
that its runner does not call `apply_recovery`, so it could not exercise this
change; its incomplete run is not counted as a result. The recovery-application
branch is verified by contract tests only.

No Wencai/provider call, Redis write, TD write, Rabbit access, service action,
or effect was performed. The change does not alter auction timing or impose a
new data-completeness gate. TASK-008 remains `PARTIAL_EVIDENCE`,
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`; M3-1 remains `BLOCKED` and
`TD_WRITE_HEALTH=UNPROVEN`.

## External recovery-owner compatibility audit (read-only)

The active `engine-next` service is configured with
`WorkingDirectory=/home/exedev/services/engine-next/current`, resolving to
`/home/exedev/services/engine-next/releases/20260903_e272842`; during this
read-only check PID `1233534` was active with `NRestarts=0`. The release
`engine_next/runtime/intraday_data_hub.py` SHA-256 is
`645ab676d921c6437b28efce3535b4ee32cac8483334610c5dc30e532f634c5b`.
The release path, source hash, PID, active state, and restart count were
rechecked read-only on 2026-10-02 and remain the same.

That release's `recover_auction_anchor(trade_date, phase)` does not accept a
Core plan, requested symbols, or requested fields. Its Wencai fallback returns
rows containing `symbol`, `change_pct`, and `amount`; it does not return the
`auction_anchor_0925_price_milli` requested by the real Core plan. The
`IntradayFetchResult` contract also has no plan ID, idempotency key,
observation-time field, or recovery revision. The fallback archives a
trade-date cohort to Redis rather than returning a requested-symbol-only
missing-field patch. Therefore this existing release path is **not** a
compatible provider for Core `RecoveryPlanV1`/`RecoveryResultV1`; it must not
be wired directly or counted as recovery execution.

The checked-out t1-v2 development repository is a different branch
(`codex/task-q2-pure-function`), and its repair tool is not the active release.
That tool's `_normalize_dataframe` extracts a `price`, but `_anchor_payload`
omits it; its Redis repair writes a filtered-effective-auction cohort, not a
Core-requested missing-symbol patch. These source observations do not prove
what a live Wencai response would contain, and no Wencai request or Redis write
was made.

Mainline impact: keep the Core recovery contract and partial facts usable, but
do not claim provider integration. A separate engine-next integration task is
needed to produce the requested per-symbol missing-field result, preserve the
existing 0925 cohort while merging only missing values, and carry source plus
observed-time metadata; `available_at` can remain UNKNOWN. This mismatch does
not block Core development on independently verified Q2 fields and is not a
new replay completeness gate.

## Idempotent recovery evidence refresh — 2026-10-02

Review of the Core-only recovery bridge found a narrow idempotency issue: a
repeat `RecoveryResultV1` correctly avoided a second content revision, but
returned the originally applied object without refreshing the latest
evaluation/observation evidence. The retry path now refreshes evidence for the
same content revision, preserves the greatest known source observation time,
does not move evaluation time backwards, and does not let an old retry rewind
a newer content revision.

Verification:

```text
targeted idempotent recovery test: PASS
full pytest: 811 passed, 3 existing protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
```

Real-data boundary remains unchanged: the pinned 2026-09-29 Q2Frame replay has
735 frames, 419,533 updates, 5,223 observed symbols, and 5,070 available / 153
missing 0925 anchor prices. It supports partial facts and a field-specific
recovery request; it does not contain a successful external recovery result.
The active `engine-next` provider contract still does not produce that requested
price field. Therefore this fix is verified at the Core contract level only;
provider execution and successful real-data recovery remain `UNPROVEN`.

No Wencai/provider call, Redis/TD write, Rabbit access, service action, or
effect was performed. A few seconds of capture-time difference remains
diagnostic context, not a global development gate. `TASK-008` stays
`PARTIAL_EVIDENCE`, replay remains usable for feature-scoped development,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, and M3-1 stays `BLOCKED`.

## Core RecoveryResult application bridge (2026-10-02)

The Core-only gap between `RecoveryResultV1` and `AuctionTimeline` is now
closed by `AuctionTimeline.apply_recovery_result()`. It checks plan/result
identity and base revision, accepts only an `APPLIED` result for the current
revision, validates requested fill scope, and records the result's source and
observation time. `RecoveryResultV1.rows` is explicitly the owner's merged
cohort, not a patch. The bridge rejects stale results, dropped source symbols,
or changes to already-available anchor prices; repeated identical results are
idempotent. A stale result does not stop the analysis timeline: existing
partial facts remain usable and a request can be regenerated for the current
revision.

This describes the original October 2 input-shape contract. The October 8
follow-up supersedes the merged-only requirement: Core now restores omitted
primary rows/fields from the saved base cohort and records anomalies, while
still rejecting conflicting rewrites and out-of-scope new facts. See
`docs/work/handoffs/TASK-008-COHORT-SOFT-FAILURE-20261008.md`.

`merge_recovery_rows()` treats zero as missing only for the three explicit
auction anchor price fields; ordinary numeric zero remains a real value. This
matches the existing Q2 anchor contract without changing `Missing != Zero`
for unrelated fields.

Verification: recovery/timeline focused tests `25 passed`; full suite
`810 passed` with 3 existing protobuf deprecation warnings; `compileall` and
`git diff --check` passed. These are Core contract tests, not provider or live
recovery evidence. The pinned real replay establishes the initial 153-symbol
0925 recovery request, while the inspected active `engine-next` provider still
does not return the requested price field. No provider call, Redis/TD write,
Rabbit access, service action, or effect was performed; `TASK-008` remains
`PARTIAL_EVIDENCE` and real recovery execution remains `UNPROVEN`.

## Per-plate opening limit-state facts — 2026-10-02

The Core per-plate opening price summary now also reports the t1-v2 Q2
`limit_state` facts for the same frozen-mapping, valid-auction, available-open
comparison cohort. The summary contract is `OpeningPlatePriceSummaryV3`.
For each plate it reports observed Up/Normal/Down counts, the valid count
denominator, total/present/missing/invalid counts, coverage, and an
`available`/`partial`/`unavailable` status. Known values remain reportable when
some values are missing or invalid; count fields are `null` if no valid state
was observed. This is `FACT_ONLY` evidence and adds no run gate.

Validation used the pinned 2026-09-29 Q2Frame, captured TD auction rows,
frozen mapping, and legacy opening report; it did not reconnect to live
Redis/TD. All 10 selected plates matched legacy for the compared limit-state
counts/status/denominators and existing opening price fields. Core opening
mismatches: 0; Core raw formula mismatches: 0. The existing, independently
documented legacy auction-median unit divergence remains (10/10 plates); it is
not changed by this addition. The final audit result and checksums are in
`/home/exedev/validation/task008-opening-plate-limit-state-20261002T003816+0800/`.

The focused tests cover complete, partial (one valid, one missing, one
invalid), and unavailable limit-state cohorts. Full verification: 812 tests
passed; compileall and diff-check passed. No service, Redis/TD, Rabbit, or
effect side effects occurred. This closes only the per-plate Core fact lane;
`TASK-008` remains `PARTIAL_EVIDENCE`, provider recovery remains `UNPROVEN`,
and normal opening acceptance remains `UNPROVEN`.

## Per-plate 0924→0925 auction-pressure sidecar — 2026-10-02

The source-defined `auction_directional_pressure_yuan` aggregate is now
carried as an explicitly separate `OpeningPlateAuctionPressureContextV1`
sidecar into `OPENING_0932`. The runner validates its date, selected-plate
scope, nested summary hash, and context hash, then includes the immutable
summary and provenance in the opening evidence. It is not injected into Q2,
the Engine state, or strategy inputs. The summary remains `FACT_ONLY`,
`full_market_coverage=UNPROVEN`, and labels the value as directional amount
delta rather than net capital flow. Historical availability stays UNKNOWN.

The source audit re-read only hash-pinned captured artifacts, not live TD or
Redis: 10 selected plate pressure values and statuses match the frozen legacy
report (`auction_pressure_mismatch_count=0`); 0924/0925 TD rows and mapping
hashes are recorded in
`/home/exedev/validation/task008-opening-pressure-integrated-20261002T010630+0800/`.
The same evidence records the known legacy median-unit divergence and does not
attest the producer binary or historical `available_at`.

Context date/scope/hash tests and runner-sidecar determinism/isolation tests
pass. A full two-pass replay of the pinned 2026-09-29 Q2Frame completed through
the new runner contract `Task008Q2FrameSessionEngineShadowV12`:

```text
input frames / symbols:       735 / 5,223
processed through 09:32:10:   734 frames / 418,759 updates
signals / reducer revision:   738 / 734
ordered/repeat deterministic: true
final Engine state hash:      59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27
```

The Q2Frame SHA remains
`5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`.
Compared with the preceding full replay on that same input, anchor evidence,
frame/update counts, signals, reducer revision, final state hash, and virtual
clock are identical. Opening summary differences are the separately intended
V3 limit-state fields and the current date-pinned price-reference context, not
an Engine-state change from the pressure sidecar. The sidecar summary hash
`500d77ca609e86843925fa01e0e1728ebd0dcddfabfc8acf56dd2aa1080f796c` is the
same in ordered/repeat output and in the captured-TD audit; the 10-plate legacy
pressure mismatch count is zero.

Evidence directory:
`/home/exedev/validation/task008-opening-pressure-integrated-20261002T010630+0800/`

- Integrated Core replay report SHA-256:
  `70d510977f7aa84a3e8384996d2db0b7117d99b060388a92f4a67b3de0f0183a`
- Pressure context SHA-256:
  `25b79753fc3f00c2cf40c15f84c86bb6f4653355c7fef429f133f6e0a27f3027`
- Audit summary SHA-256:
  `640f42129f046db46766e2e1f8124c22ebec54d5aaa9469eee1a7fc793e601d7`

Final full suite: `816 passed`, 3 existing protobuf deprecation warnings;
compileall and diff-check pass. The production-import boundary scan found no
Redis/TD/Rabbit clients or write/ACK calls in the pure aggregator, runner, or
file-only audit path. No live Redis/TD/Rabbit access, write, service action,
ACK, or effect occurred. Captured TD completeness, full-market coverage,
historical `available_at`, and producer build identity remain unproven. This
is a useful feature-development evidence slice, not `NORMAL` acceptance;
TASK-008 stays `PARTIAL_EVIDENCE` and normal opening acceptance remains
`UNPROVEN`.

## Active-release auction-pressure formula and denominator audit (2026-10-02)

Added `examples/audit_task008_auction_pressure_production_parity.py` as a
repeatable, file-only audit. It reads the existing hash-pinned 2026-09-29
captured TD rows and frozen stock/plate mapping, then invokes the matching pure
helpers from the hash-verified active `engine-next` release. It does not query
live TD/Redis, touch Rabbit, or call a production service.

On the 5,223 common symbols, Core and active-release anchor helpers exactly
matched all compared 0924→0925 formula outputs: status, amount/price/book
deltas, pressure delta, amount ratio, withdrawal, direction, directional
pressure, labels, and reference buckets. At plate level, all 10 selected
pressure sums and all 10 pressure statuses matched this captured sample.
However, 8/10 plate quality denominators differ:

| Plate | Frozen mapping / Core denominator | Release observed-row count | Not observed in captured rows |
| --- | ---: | ---: | ---: |
| 光模块 | 36 | 35 | 1 |
| 农业 | 33 | 29 | 4 |
| 医药 | 343 | 320 | 23 |
| 并购重组 | 86 | 84 | 2 |
| 房地产 | 39 | 38 | 1 |
| 机器人 | 564 | 514 | 50 |
| 芯片 | 166 | 163 | 3 |
| 通信 | 107 | 100 | 7 |
| 风电 | 17 | 17 | 0 |
| 黄金 | 21 | 21 | 0 |

Thus the result is `VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE`, not full
production parity. The 91 unmapped-in-observed-cohort members are visible in
Core's full frozen-mapping denominator; the release's legacy status is based
on symbols present in the captured rows. The observed status agreement
(`partial` for these 10 plates) is sample-specific and must not be generalized
to other dates/cohorts. This finding is not a reason to weaken Core's
denominator or change either path silently; it defines the scope difference
that any future product-facing status contract must resolve explicitly.

The audit was run twice from the same pinned files. Its deterministic
`audit_summary.json` SHA-256 was
`3e28ef7e3206e94758f2fa3549c5f3dcabee1e1802a8f1eec85f5657350163f1` in both
runs. The primary validation directory is
`/home/exedev/validation/task008-auction-pressure-release-parity-20261002T020200+0800/`;
the repeat is under
`/home/exedev/validation/task008-auction-pressure-release-parity-repeat-20261002T020200+0800/`.
The analyzer has four unit tests for per-symbol comparison and mismatch
precedence. Full suite: `820 passed`, with the same three protobuf deprecation
warnings; compileall, diff-check, and evidence checksums passed.

Limits remain: this reuses captured historical SELECT evidence, not a new live
read; TD completeness, historical `available_at`, Rabbit arrival/freeze order,
and the exact deployed producer binary remain unproven. It verifies pure
formula parity and clarifies denominator semantics only. `TASK-008` remains
`PARTIAL_EVIDENCE`; `NORMAL_OPENING_ACCEPTANCE=UNPROVEN` and M3-1 remains
`BLOCKED`.

## Same-date 09:25 effective-universe gate audit (2026-10-02)

The preceding 2026-09-29 pressure audit compares pure formula helpers only;
it does not run the full `engine-next` assembly gate. Its corrected repeatable
report now says `production_assembly_gate=NOT_REPLAYED_MISSING_SAME_DATE_REDIS_ANCHOR`.
Do not interpret the 09-29 helper comparison as proof of the user-facing
production report status.

A separate file-only audit applied the exact hash-pinned active-release
`normalize_td_auction_row` and `_classify_td_inactivity` functions to the
hash-verified 2026-09-30 09:25 Redis anchor and TD capture:

- Redis anchor: 5,210 symbols; TD: 5,220 rows and unique symbols.
- TD-only: 10 symbols; anchor-only: 0.
- All 10 TD-only rows have `match_amt_yuan=0`, `rest_bid_amt_yuan=0`,
  `rest_ask_amt_yuan=0`, and `chg_bp=null`.
- The active release classifier requires all four fields to be present and
  finite before it returns `inactive`; it classifies these 10 as `unknown`.
- No row is excluded, leaving 5,220 effective TD symbols against 5,210 Redis
  anchor symbols: `effective_universe_status=mismatch`.

In the inspected release source, a mismatch makes `universe_valid=false` and
sets `plate_facts_status=unavailable`. This establishes the exact conditional
code-path result for the captured same-date inputs. **The full assembler was
not invoked and a final 09:26 production report was not captured.** The capture
contains only the 09:25 cohort, not the complete 09:20/09:24/09:25 rows,
mapping snapshot, or market summary needed to replay the entire assembly.
Therefore record this as `GATE_MISMATCH_REPRODUCED_FROM_CAPTURE`, not as proof
that a user-facing report was actually suppressed on 2026-09-30.

The audit used only sealed local files and the active release source; it made
no live Redis/TD/Rabbit connection, service change, write, ACK, or effect. The
active symlink still resolved to release `20260903_e272842`; `engine-next` was
`active`, PID 1667556, `NRestarts=0` at the read-only recheck. This does not
attest which source built the running executable.

Primary and repeat evidence directories:

- `/home/exedev/validation/task008-production-universe-gate-20261002T023000+0800/`
- `/home/exedev/validation/task008-production-universe-gate-repeat-20261002T023000+0800/`

Both reports have the same semantic evidence hash
`a674b0c7b37da504f5c9d7bfbb9a190974e57fb79da78c1ab3e5623714d9cce8`; both
`sha256sum -c sha256sums.txt` checks pass. The first exploratory output at
`task008-production-universe-gate-20261002T022437+0800` is superseded: it
included raw Redis `top_amount` payload and a full symbol list. Use only the
compact 02:30 reports as the reviewed evidence.

The corrected 2026-09-29 pressure helper audit was also rerun twice using the
current script. Both audit summaries hash to
`e7c9817877c24ad34769efcc8d3687e4a0efc91960cfc4b71d107999bbc923c2` and report
`VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE`; the assembly gate remains explicitly
`NOT_REPLAYED_MISSING_SAME_DATE_REDIS_ANCHOR`. Evidence directories:

- `/home/exedev/validation/task008-auction-pressure-release-parity-20261002T022640+0800/`
- `/home/exedev/validation/task008-auction-pressure-release-parity-repeat-20261002T023000+0800/`

Targeted parity and universe-gate tests: `12 passed`. Final Core verification:
`828 passed` with 3 existing protobuf deprecation warnings; compileall and
`git diff --check` pass. Both universe-gate report checksum manifests and both
corrected pressure-audit checksum manifests verify. Production release source
and systemd status were inspected read-only; no production source/service/data
was changed and no live Redis/TD/Rabbit operation, ACK, or effect occurred.
`TASK-008` remains
`PARTIAL_EVIDENCE`; `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`; M3-1 remains
`BLOCKED` / `TD_WRITE_HEALTH=UNPROVEN`. The next useful evidence is a same-date
2026-09-30 Core replay/assembly comparison using a complete captured input
cohort—not tighter second-level timestamp equality. Keep unknown rows visible
as partial/unknown facts; do not silently alter the production gate or claim
the final report was suppressed without capturing that report.

## Current-worktree same-date Q2Frame replay (2026-10-02)

The already-frozen t1-v2 output
`/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/q2frame.jsonl`
was rerun through the current Core worktree. Its input SHA-256 matched the
pinned value `10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`.
The run completed in approximately half an hour; elapsed time is recorded as
observation, not a pass/fail threshold. Output:
`/home/exedev/validation/task008-current-core-same-day-20260930-20261002T023500+0800/replay.json`
(SHA-256 `277376255f4f7a33c2eacee7fd92cfaa39c403c8f7a02ded0c2b914b8f993f59`,
verified by its `sha256sums.txt`).

Ordered and repeat passes were deterministic across all emitted comparison
fields: 603 frames, 199,621 updates, 5,220 symbols, one Engine, 606 processed
signals, reducer revision 603, and virtual clock `1790731506000` (09:25:06
Asia/Shanghai). Both passes produced final state hash
`ecbced7e05c5349f544d96d148e72946ba4a08e3502e7d73c831c604fe501eb7`.
The same final-state hash was produced by the previous 2026-10-01 current-Core
run. The 09:25 anchor reports 5,030 available and 190 missing anchor prices;
the recovery plan requests exactly those 190 symbols and now names the missing
field `auction_anchor_0925_price_milli`. The 09:20/09:24 evidence is unchanged;
the only cross-run anchor-evidence difference is this more precise 09:25
recovery-plan field label/content hash.

This verifies the current Core against the frozen real Q2Frame for that date's
available period. The input ends at 09:25:02, so this run has empty
`opening_evidence` and does not verify 09:32 opening behavior or
`NORMAL_OPENING_ACCEPTANCE` (which remains `UNPROVEN`). It was file-only and
in-memory: no live Redis/TD/Rabbit access, production write, ACK, service action,
or effect. The 2026-09-30 effective-universe mismatch described above remains
an independent production-gate finding; deterministic replay does not resolve
that gate or prove a user-facing report was suppressed.

## Current-Core 09:32 replay and producer-summary comparison (2026-10-02)

The 2026-09-29 archived t1-v2 Q2Frame was replayed through the current Core
worktree with `--include-opening`; the input is the real TD-derived 3-second
replay artifact, not a synthetic fixture. Source replay log records 416,870
TD rows, `source_reject=0`, `ack=0`, `td_sql=0`, and 419,533 committed
quote-state updates. Input Q2Frame SHA-256 is
`5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`.

The current Core output is
`/home/exedev/validation/task008-current-core-opening-20260929-20261002T031100+0800/replay.json`
(SHA-256 `9840230848136e33c2d88300c68ede8ee3a13400416f6667fc9c9fbc2972c819`).
Ordered and repeat passes agree across all emitted determinism checks. They
process 734 frames / 418,759 updates through 09:32:10 with one Engine; the
09:32:11 frame is the first excluded frame. Each run has 738 processed
signals, reducer revision 734, and final-state hash
`59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27`.

At opening, the current Core reports 5,211 `READY` and 12 `PARTIAL` facts.
Those 12 are stale observed quotes. Coverage is scoped to the 5,223-symbol
Q2Frame cohort, not full market; `NORMAL_OPENING_ACCEPTANCE=NOT_EVALUATED`.
For all 5,223 symbols, the public per-symbol opening fields
(`amount_2m_yuan`, `change_pct`, `limit_state`, its status, name, speed,
status, symbol, timestamp) match the older same-input Core output exactly.
The current output additionally emits explicit cross-section, amount,
limit-state, and opening-transition summaries; do not treat the changed
aggregate/final hashes by themselves as a per-symbol value mismatch.

The same t1-v2 replay archive contains the producer's 09:20/09:24/09:25
Redis-command summaries. Core `AuctionAnchorFactV1` available counts match the
producer `valid_stock_count`: 1,064 / 3,137 / 5,070. Applying the Core
`ANY_POSITIVE(am,br,ar)` candidate rule to the latest Q2Frame cohort yields
4,773 / 5,057 / 5,205 candidates, exactly the producer `total_stocks` counts.
Core's `MISSING` counts (4,159 / 2,086 / 153) are against all 5,223 observed
Q2 symbols, while producer `unavailable_stock_count` (3,709 / 1,920 / 135) is
within its candidate universe. The differences 450 / 166 / 18 are exactly the
non-candidate symbols, not price-value mismatches. In the runner output,
`auction_anchor_fact_status_counts` is the anchor's field quality; the
separate `fact_status_counts` is the shadow strategy's readiness state
(`PENDING` before enough anchor segments exist). Do not conflate them.

For the 09:25 aggregate, 11 compared numeric values match exactly between
Core and the archived t1-v2 producer summary: candidate/total 5,205; valid /
unavailable anchor prices 5,070 / 135; positive / negative / flat 1,443 /
2,849 / 778; limit-up / limit-down 7 / 6; auction amount 11,368,130,630 yuan;
and limit-up bid amount 32,232,283 yuan. Core's Q2 aggregate observation is
09:25:02, versus the producer snapshot metadata at 09:25:06. That 4-second
difference is recorded, not made a failure gate; the compared values match.
The producer's ordered top-200 exists, but current Core does not emit the same
ranked list, so top-200 ordering parity is `NOT_COMPARABLE`.

The archive's 09:32 `atomic_cutoff_publish` payload gives a direct row-level
comparison target. Its `opening_cutoff_v1` metadata reports 5,211 accepted
rows and `universe_authority_status=partial`. For the 5,211 shared symbols,
Core matches `amount_2m_yuan` and `limit_state` in every row; Core's
`change_pct` matches `((px_milli / pc_milli) - 1) * 100` in every row as well
(0 invalid price pairs; maximum absolute difference 0). Source timestamps are
exact for 5,207 rows; the remaining four differ by exactly 3 seconds. Per the
project's stated purpose, that timing delta is observed but not a failure
gate.

Core contains 12 additional observed symbols absent from the producer payload.
Their source ages at 09:32:10 are 1,009–1,030 seconds, compared with 0–37
seconds for the shared payload rows. This aligns with the aggregate Core
opening status of 5,211 `READY` / 12 `PARTIAL`; those 12 remain represented in
Core rather than silently becoming zero or blocking the calculation. The
producer snapshot itself declares universe authority `partial`; this is not a
full-market claim. Full row-level comparison summary:
`/home/exedev/validation/task008-current-core-opening-20260929-20261002T031100+0800/opening_cutoff_parity.json`.
The command payload is an archived t1-v2 replay output, not a fresh live Redis
read.

Machine-readable comparison and all source/code checksums are in
`/home/exedev/validation/task008-current-core-opening-20260929-20261002T031100+0800/auction_summary_parity.json`
and `sha256sums.txt`. Checksum verification passed. This remains historical
event-time replay—not Rabbit arrival order, live Redis comparison, or NORMAL
acceptance. No Redis/TD/Rabbit connection, write, ACK, service action, or
effect occurred during the current-Core run. `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

Historical tool-path note (2026-10-09): the interim audit helper
`examples/audit_task008_auction_pressure_production_parity.py` and its paired
test were later moved out of the Core repository into
`/home/exedev/validation/task008-tools-archive-20261009T1355+0800/`.
The archive manifest records their checksums and restoration paths; this
contemporaneous report describes the tool location at the time of that run.
