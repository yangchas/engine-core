# Current Task

## Latest bounded repair — remove row-chunk contract drift (2026-09-25)

t1-v2 development commit `dde58d64b530f5eec65a34c3254ec6fcb7f930d0` removed
the unused `REPLAY_BATCH_SIZE` reserve setting and `chunk_no` metadata, and
renamed slice completion metadata to `slice_final_phase`. The current TD
source issues one SELECT per half-open 3-second slice and retains every row;
real 2026-09-23 checks processed 5,071 rows in one ordinary slice and 906 rows
in two in-memory event-time phases for a slice crossing the 09:24:10 barrier.
The latter still emitted one final Q2Frame. C++ full build/self-test and Core
Q2Frame readback passed. No Redis/TD write, Rabbit consume/ACK, service change,
or deployment occurred.

This closes only row-count chunking/configuration drift. Barrier-crossing slices
still yield multiple Engine batch calls so the business snapshot is captured at
its event-time barrier; therefore exact one-Engine-batch-per-slice and Rabbit
delivery/batch equivalence are not proven. Return to the existing mainline:
keep Phase P/TASK-008 `PARTIAL`, do not promote a task or start strategy work.
Audit: `docs/work/handoffs/TD_RABBIT_NO_ROW_CHUNK_REPAIR_20260925.md`.

```text
active_task: TASK-008
bootstrap_status: COMPLETE
last_task: TASK-007
last_task_state: MERGED
next_task: TASK-008 real opening validation
next_task_state: RUNNING / PARTIAL_EVIDENCE

owner: replay-investigator
branch: codex/feature-session-engine-integration
worktree: current development worktree
started_at: 2026-09-20T14:10:36+08:00
fix_started_at: 2026-09-20T14:46:36+08:00
implementation_commit: 084d6819b31a80087d624cfabf0d78843c8613ba
latest_fix_commit: aa38614

## Latest Phase P audit — source-aligned barrier trace (2026-09-25)

A real TD replay of `2026-09-23 [09:15:00,09:25:09)` captured all 5,222
per-symbol states at 09:20:03, 09:24:10 and the 09:25:06 Clock barrier. The
5,222 Q2 hashes and active set, all A2/legacy 0920/0924/0925 projections, and
the 0925 anchor matched DB5/`task009k:`; the trace's 5,208 candidate symbols
matched the Redis anchor member set. No future source timestamps were included.
Run: `212022` ticks, `204` batches, one Clock, `td_sql=0`, `ack=0`; Redis
writes were only DB15/`phasepdiag20260925T0840:` and DB0 had zero prefix hits.
The replay-only trace code/test was committed locally in t1-v2 as `5c61f43`
(not pushed or deployed); full-dependency build/self-test passed. This closes
the bounded current-source output mismatch for this one date/window, not Phase
P/TASK-008: Rabbit delivery/arrival, same-timestamp source ordering and
historical `available_at` remain unknown; M3-1 remains blocked. Audit:
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_TRACE_AUDIT_20260925.md`.

## Phase A exact runtime release refresh + bounded Redis retry repair (2026-09-25)

Read-only audit re-verified the active release binary and found Rabbit outer/inner
batch metadata is dropped before `TickBatch`; Rabbit arrival/member ordering and
historical `available_at` remain unknown. It also found a frozen A20/A24/A25
Redis command could be lost after a failed one-shot trigger. A process-local
retry cache was added to the t1-v2 development branch as
`f4c3eb50056d7d1faa4cd1bcfe2fd7ce71da6e51` (not pushed, merged, or deployed).
Failure injection, full t1-v2 build/self-test, Core `691 passed`, compileall and
diff-check passed. The test used fake Redis/Null TD; it did not write real Redis
or TD. This is not a durable outbox and does not resolve TD dual-write retry.
Phase A is `PASS_WITH_LIMITS`; Phase P/TASK-008 remain `PARTIAL`, M3-1 remains
blocked. Full matrix and boundary notes:
`docs/work/handoffs/TD_RABBIT_RUNTIME_RELEASE_AUDIT_20260925.md`.

## Independent audit — Phase P auction-close fix (2026-09-25)

A second bounded real-data replay into isolated Redis DB15 matched the prior
fixed-run Q2 and auction outputs (5,222 active symbols; Q2 `am` sum
`13,621,401,036`). Only run telemetry `m2:runtime.redis_bytes` differed, by
the expected key-prefix length. A follow-up isolated replay exited `0` with
212,027 input/ticks, 222 batches, one clock, `td_sql=0`, and `ack=0`. This is
`PASS_WITH_LIMITS`, not full producer or Rabbit equivalence. Phase P/TASK-008
remain partial; M3-1 remains blocked. Audit:
`docs/work/handoffs/TD_RABBIT_PHASE_P_AUCTION_CLOSE_INDEPENDENT_AUDIT_20260925.md`.

## Latest update — Phase P auction-close empty-slice fix (2026-09-25)

Real TD for 2026-09-23 contained zero rows in `[09:25:57,09:26:00)` and five
rows at exactly `09:26:00.000` in the next half-open slice. The current-source
replay assigned the empty preceding slice its excluded right-edge time, so it
emitted `latest` from stale pre-close values and the throttle suppressed the
summary after those five rows. t1-v2 commit `9472f4c` now keeps empty frames
inside their half-open interval, groups 09:26:00 rows at an auction-close
barrier, and forces the final latest snapshot. On the same real 212,027-row
bounded replay, `latest.total_auction_amount_yuan` now equals Q2 `am` sum
`13,621,401,036`; the 5,222 Q2 hashes and frozen 0920/0924/0925 outputs are
unchanged. Self-tests and full build passed; the replay wrote only to a unique
Redis DB15 prefix (`td_sql=0`, `ack=0`). Handoff:
`docs/work/handoffs/TD_RABBIT_PHASE_P_AUCTION_CLOSE_EMPTY_SLICE_20260925.md`.

This closes only the observed 09:26 boundary defect. Phase P and TASK-008
remain `PARTIAL`; Rabbit delivery/arrival, historical `available_at`, and
NORMAL acceptance are not proven. Do not advance a phase from this result.

## Latest update — Phase P same-second barrier closure (2026-09-25)

The replay barrier defect is fixed and committed in the t1-v2 development
repository as `acf277bbba0d3bab90aa6550a23850e2c8aa7013` (not pushed, merged, or
deployed). The final full build/self-test passed. A real TD replay of
`2026-09-23 [09:15:00,09:25:09)` processed 212022 ticks, 204 market batches,
one separate 09:25:06 Clock control event, wrote Redis only to a unique DB15
prefix, and emitted zero TD SQL or Rabbit ACKs. Current-dev anchor counts and
times align with DB5, but its Q2/A2 content differs because its calculator
source is not release-equivalent. Exact Q2/A2 parity was independently
re-confirmed on the release-calculation/current-reader validation copy after
the same barrier fix.

This closes the narrow same-second barrier issue only. Phase P remains
`PARTIAL`; current-dev full projection/member-set parity, Rabbit membership or
arrival order, historical `available_at`, and M3-1 are not passed. Do not
advance to strategy migration from this evidence. Detailed phase audit:
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_SECOND_INCLUSIVE_20260925.md`.

## Latest TD/Rabbit Phase P source replay audit — 2026-09-25

The current t1-v2 development-source barrier implementation is committed
locally as `stock-situation-runtime` commit `e91a20a` (not pushed/deployed).
Three real 2026-09-23 `[09:15:00,09:25:09)` TD replays wrote only isolated
Redis DB15 namespaces. Successful run counters were 212022 input/ticks, zero
rejects, zero TD SQL writes and zero Rabbit ACKs; all three Redis namespaces
were semantically identical after excluding only global `redis_bytes`. Core
readback was 5222/5222, coverage 1.0, no missing, 154 stale, `PARTIAL`.

This corrects the prior validation-only conclusion: current-source output is
not fully equal to old DB5/`task009k:`. Q2 has 5206 hash-value differences;
0920/0924 A2 counts each differ by one; 0925 count is equal. Root cause remains
UNKNOWN. Replay repeatability is proven for this build; old-baseline parity,
Rabbit delivery/arrival, historical `available_at`, and NORMAL acceptance are
not. Phase P remains PARTIAL; do not advance this task or M3-1 from this result.
Details: `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_CORRECTED_AUDIT_20260925.md`.

Follow-up source alignment audit found that development commit `e91a20a` is not
calculation-source equivalent to deployed release `20260923_tdstop0945b`:
auction matching/rest formulas, limit-state reference price, and Engine clock /
session handling differ. The release metadata names base commit `9fd4a42`, which
is unavailable in the local t1-v2 Git object database; that repo has no remote
configured. A validation-only hybrid then used the release calculation files
with the current 3-second TD reader/barrier on the same real 2026-09-23 window,
writing only to an isolated DB15 Redis prefix. All 5222 Q2 hashes and the Q2
active set exactly matched DB5/`task009k:` (aggregate SHA-256
`308a928e...5b9ff04f`). Auction parity is partial: 0925 A2/legacy output and
the 0925 anchor match, but 0920 and 0924 are each short by one member
(`4872 vs 4873`, `5099 vs 5100`) and their ranked/summary payloads differ.
The `latest` timestamp is run-specific. This narrows the remaining issue to
auction barrier membership/timing or related reader behavior; it does not
prove which one. Phase P remains `PARTIAL`. Details:
`docs/work/handoffs/TD_T1V2_SOURCE_ALIGNMENT_AUDIT_20260925.md` and
`docs/work/handoffs/TD_T1V2_SOURCE_ALIGNED_HYBRID_REPLAY_20260925.md`.

TASK-004, TASK-005, TASK-006 and TASK-007 passed integrator review on 2026-09-20.
TASK-004 remains `PASS_WITH_WARN` for the 5–10 minute benchmark band; this
does not change the production gate. See
`docs/work/handoffs/INTEGRATOR_REVIEW_20260920.md`.

TASK-007 implementation was merged, then its acceptance was reopened by a
read-only audit. Fixes are limited to the offline canonical replay/auction-facts seam described in
`docs/work/plans/TASK-007-offline-canonical-auction-facts.md`.

The latest fix keeps timing-derived node state in evidence rather than source
semantic identity. Full verification is green (`682 passed`, compileall and
diff-check PASS). Offline tester handoff is PASS. A dedicated real TD ordered
replay has now completed 500 canonical frames (1,224,811 rows, 98 empty frames,
18.07 minutes) with no production side effects. A matching 500-frame shuffled
pass also completed (19.25 minutes), with all comparison hashes equal. The
functional result is PASS;
the performance target is not met and needs optimization, but elapsed time is
not treated as a data or replay correctness failure. A 20-frame real
ordered/shuffled determinism is PASS for the complete 500-frame window. See
`docs/work/handoffs/TASK-007-REAL-DATA-20260920.md`.

The latest no-semantic-change hot-path optimization was measured against real
TD data with `FINAL` verification:

```text
validation: /home/exedev/validation/task007-perf-final-500-hotpath-20260920T172951+0800
500 frames / 1,224,811 rows / 98 empty frames
500 signals / reducer revision 500 / VirtualClock 09:40
811,534.946 ms (13.53 min)
session and final hashes equal to the prior FINAL baseline
```

This is performance evidence, not a new functional acceptance gate. The full
ordered/shuffled determinism evidence remains the earlier FULL validation; the
optimized run is ordered FINAL parity/performance evidence. Details are in
`docs/work/handoffs/TASK-007-PERFORMANCE-20260920.md`.

A further local read-only boundary audit passed, including evidence checks and
the public hash-field serialization compatibility fix; it is recorded in
`docs/work/handoffs/TASK-007-READONLY-AUDIT-20260920.md`.

The independent read-only auditor returned `AUDIT_STATUS=PASS`,
`BLOCKING_FINDINGS=NONE`, and `MERGE_RECOMMENDATION=MERGE`. The final sign-off
is recorded in `docs/work/handoffs/TASK-007-INDEPENDENT-AUDIT-20260920.md`.
TASK-007 is therefore `MERGED`. The slow runtime remains a non-blocking
optimization item; it is not a functional replay failure and does not weaken
the real-data evidence.

TASK-008 has now started as a bounded real-data validation:

```text
TASK-008: real opening validation
state: RUNNING / PARTIAL_EVIDENCE
plan: `docs/work/plans/TASK-008-opening-validation.md`
handoff: `docs/work/handoffs/TASK-008-REAL-DATA-20260920.md`
audit: `docs/work/handoffs/TASK-008-AUDIT-20260920.md`
replay_handoff: `docs/work/handoffs/TASK-008-REPLAY-REAL-DATA-20260920.md`
NORMAL opening acceptance remains UNPROVEN until a controlled 2026-09-18
09:32:10 observation or equivalent historical available_at evidence exists. A
target-date frozen Redis Q2 capture was replayed for determinism, but 5,219
source rows are future relative to that cutoff; see the replay handoff. This is
`REPLAY_PARTIAL`, not NORMAL acceptance.
```

The producer boundary is explicit: t1-v2 generates Q2; Core does not derive
Q2 from TD ticks. The earlier 09:15–09:40 TD replay remains tick-layer
evidence only. The same-input t1-v2 dry-run Q2Frame artifact is the required
bridge evidence for TASK-008; Q2Frame source timestamps are accepted without a
market-hours gate, while freshness and historical availability remain separate
evidence policies. NORMAL opening acceptance still requires its own cutoff
evidence.

The earlier strict artifact under `/home/exedev/validation/task008-q2frame-real-20260923/`
is a 500-frame, slice-boundary contract check; it must not be described as the
exact t1-v2 delivery shape. The exact current t1-v2 release was then run on the
real 09:15–09:40 TD window in local `--q2frame` mode. It consumed 1,224,811
rows and emitted 1,205 source-time batches (t1-v2 groups equal `tss`, so it does
not emit one batch per 3-second read slice), 1,226,603 Q2 updates and 98 clock
events without Redis/TD writes. Core consumed that frozen file twice and
returned `REPLAY_READY_BOUNDED`; all determinism fields matched. Evidence:
`/home/exedev/validation/td-rabbit-phase-c-q2-full-20260924T014403+0800/phase_c_full_report.md`.
This closes the exact full-window TD→t1-v2→Core Q2 bridge, but does not prove
NORMAL opening acceptance, Rabbit delivery equivalence, or historical
`available_at`; those remain `UNPROVEN`.

The detailed TD/Rabbit global replay plan is
`docs/work/plans/TD_RABBIT_GLOBAL_3S_REPLAY.md`. Its current state is
`PHASE_P_PARTIAL`: a full-window exact t1-v2 replay with
`REPLAY_BATCH_SIZE=1000000` still emitted 1224 `tss`-group batches rather than
one batch per 3-second slice. Its 1194572 real ticks, 5222 Q2 symbols, stable
auction outputs, isolated Redis readback, and Core ordered/shuffled hashes
matched Phase O exactly; DB0 stayed untouched. This closes another real Q2
repeatability check but leaves the Rabbit-shaped batch contract open. See
`docs/work/handoffs/TD_RABBIT_PHASE_P_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-p-single-slice-20260924T232137629+0800/`.

A validation-only barrier-aware whole-slice experiment then used real
2026-09-24 TD `[09:15:00,09:25:09)` and isolated Redis DB13. One 3-second
SELECT remained the input boundary; only business barriers (`09:20:03`,
`09:24:10`, `09:25:06`, `09:32:10`) divided processing order. The run processed
210730 ticks in 204 batches plus one Clock, with `td_sql=0` and `ack=0`.
Q2, 0920/0924/0925, latest, and 0925 anchor semantic hashes matched Phase M;
Core readback was deterministic with coverage 1.0 and 549 stale quotes, so it
remains `PARTIAL`/`FACT_ONLY`. This closes the observed logical-time look-ahead
for one real date, but not Rabbit delivery/arrival or historical `available_at`.
Handoff: `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_EXPERIMENT_20260925.md`.

第二个真实日期复测随后完成：2026-09-23 `[09:15:00,09:25:09)` 使用同一
validation barrier binary 写入隔离 Redis DB14/`task009pbarrier23:`，203 个
3 秒片合计 212022 行，1 个空片，24 个片有同股多事件时间；t1-v2 产生 204
个 batch 加一枚 Clock，`td_sql=0`、`ack=0`。Q2、0920/0924/0925/latest/
anchor/A2 均与 DB5/`task009k:` 基准相等；Core 同一观察时刻读回 coverage
1.0、missing 0、stale 154，重复 hash 相等。两个真实日期均支持片内业务
屏障假设，但 TASK-009/Phase P 仍为 `PHASE_P_PARTIAL`，Rabbit delivery/arrival、
historical `available_at` 和 NORMAL opening 未证明。详见
`docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER23_EXPERIMENT_20260925.md`。

The prior Phase O state remains historical: full-window t1-v2 Q2Frame and Core repeat replay are real
and deterministic; bounded real TD→t1-v2 replays have now written isolated
Redis DB10/DB11/DB12/DB13/DB14/DB15 and verified the 09:25:06 empty-slice Clock
and Core projection. Phase G covered 09:20:00–09:25:09 with 129281 real ticks,
305 batches, 5209 Q2 hashes, and a deterministic repeat after normalizing the
isolated prefix. 09:25 price/change fields match comparable TD snapshot rows,
but amount/rest fields remain partial pending same-version semantic proof.
Rabbit delivery membership/arrival, live-barrier visibility and historical
`available_at` remain open. Do not advance TASK-008 to NORMAL acceptance from
this evidence alone. See
`docs/work/handoffs/TD_RABBIT_PHASE_G_AUDIT_20260924.md`.

Follow-up source-time audit found that 5184/5221 real 0925 snapshot rows match
the current t1-v2 formula at some earlier TD tick, while only 3576/5221 match
the event-time latest tick before 09:25:06. This supports a visibility/arrival
set difference hypothesis, not a license to substitute the old snapshot into
replay. Rabbit arrival and historical `available_at` remain `UNKNOWN`; see
`/home/exedev/validation/td-rabbit-phase-h-snapshot-source-20260924T200951431+0800/`.

A cross-day real Redis repeat was then completed for 2026-09-23
09:24:00–09:25:09 using the exact current t1-v2 release. The run processed
44276 real TD ticks in 64 source-time batches plus one clock, wrote 89010
Redis commands to isolated DB9/`task009i:`, and recorded `td_sql=0` and
`ack=0`. A repeat on isolated DB8/`task009i2:` had 5215 keys and identical
normalized semantic content. Core read 5206 real Q2 hashes and obtained equal
ordered/shuffled projection hashes; the result remains `REPLAY_PARTIAL` and
`normal_opening_pass=UNPROVEN`. The corresponding 0925 TD snapshot has 5222
rows, 16 of whose symbols are absent from the replay window; this is recorded
as a real source-set difference, not silently filled or treated as a t1-v2
failure. Evidence and checksums are in
`docs/work/handoffs/TD_RABBIT_PHASE_I_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-i-0923-0924-0925-20260924T201547736+0800/`.

The follow-up 09:15-baseline run for the same date replayed
`09:15:00–09:25:09` through the exact current t1-v2 release into isolated
Redis DB5/DB4. It processed 212022 real ticks in 604 batches with one Clock,
committed 428284 Redis commands, and recorded `td_sql=0`, `ack=0`. Both runs
produced 5222 Q2 symbols and identical normalized semantic content. Unlike the
09:20-start run, the TD source set, Q2 set, and 0925 snapshot set were all
5222; the prior 16-symbol difference was therefore a replay-start boundary
issue, not a producer drop. Core readback remained deterministic but reported
17 stale baseline quotes under the explicit 10-second freshness policy, so the
result is still `REPLAY_PARTIAL` and `normal_opening_pass=UNPROVEN`. t1-v2's
0925 A2 metadata reported `n=5208` versus 5222 Q2 hashes. The 14 Q2 symbols
absent from the A2 anchor correspond exactly to old 0925 snapshot rows with
`px/chg=NULL` and `match/rest=0`, so they are unavailable facts rather than
Q2 loss. For the remaining 5208 symbols, chg/match/rest bid/rest ask matched
5208/5208 and comparable price matched 5068/5068; 140 NULL-price rows retain
explicit unavailable semantics. Numeric anchor/field parity therefore remains
`PARTIAL_WITH_EXPLICIT_UNAVAILABLE`, not a fabricated full PASS. Evidence is in
`docs/work/handoffs/TD_RABBIT_PHASE_K_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-k-0923-0915-0925-20260924T202721825+0800/`.

The full real 2026-09-23 `09:15:00–09:40:00` window was then replayed through
the exact t1-v2 release with Redis writes isolated to DB3, and repeated on DB2.
Both runs processed 1204178 TD ticks in 1224 batches with 78 clocks and
2430075 Redis commands, with `td_sql=0` and `ack=0`; normalized Redis semantic
content matched exactly. The 0920/0924/0925 A2 and anchor keys matched the
shorter 09:25 cutoff run byte-for-byte after normalization, proving later
rolling Q2 did not overwrite frozen auction outputs. The final all-day Q2
capture was independently read back through Core: 5222 rows, coverage=1.0,
equal ordered/shuffled projection hashes and equal sampled Engine hashes.
Core classified the result as `REPLAY_PARTIAL` because 68 quotes were stale
under the explicit 10-second freshness policy; normal opening remains
`UNPROVEN`. Evidence is in
  `docs/work/handoffs/TD_RABBIT_PHASE_L_AUDIT_20260924.md` and
  `/home/exedev/validation/td-rabbit-phase-l-0923-full-20260924T203942122+0800/`.
The independent live-log audit is recorded in
`docs/work/handoffs/TD_RABBIT_LIVE_LOG_AUDIT_20260924.md`; it confirms
progress/ACK counters only and leaves Rabbit delivery membership, arrival order
and completion watermark `UNKNOWN`.

Verification for this boundary correction: `687 passed`, compileall and
diff-check PASS. The producer-bridge tooling lives in
`examples/run_task008_t1v2_q2frame_replay.py` and
`examples/aggregate_t1_v2_q2frame_3s.py`; it reads and writes local JSONL
artifacts only.
```

TASK-003 established the streamed cross-sectional foundation and performance
instrumentation. TASK-004 completed the incremental identity/Q2 optimization
without weakening the replay contract. The 500-frame ordered FRAME benchmark
processed 1,224,811 events in about 458.8 seconds, and the ordered FINAL
benchmark completed in about 453.0 seconds with final FULL parity PASS. Both
FRAME passes completed in about 454.4/456.4 seconds with deterministic
equality. Each full pass is in the 5–10 minute `PASS_WITH_WARN` band; this is
not a clean <=5 minute performance PASS.

The FRAME runs intentionally report per-frame incremental identity only;
FULL cross-section parity is not claimed for every frame. The FINAL run is the
parity evidence. The shuffled pass changes event order only within each frame;
it is not a Rabbit arrival-order simulation.

The current production gate remains:

```text
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

TASK-004 was accepted with its recorded `PASS_WITH_WARN` performance status.
TASK-005 is merged as a pure in-memory `ReplaySessionTimeline`. TASK-006 is
merged as a RabbitMQ-primary canonical `MarketTickV1`/`TickBatchV1` contract:
Rabbit's parsed `DataRecord/DataBatch → RawTick/TickBatch` shape is authoritative
and TD remains a pure compatibility adapter. No Rabbit/TD/Redis/effect path was
added. The production gate remains independent.

Phase M then replayed the same-day real TD window `2026-09-24
09:15:00–09:25:09` through the exact current t1-v2 binary into isolated Redis
DB1/`task009m:`. Independent TD count and t1-v2 `source_in/ticks` were both
210730; the run produced 604 batches, one 09:25:06 Clock, `td_sql=0`, `ack=0`,
and 425676 Redis commands. Core read back 5222 Q2 hashes with coverage 1.0 and
equal ordered/shuffled projection plus sampled Engine hashes; 549 quotes were
stale under the explicit 10-second policy, so this remains `REPLAY_PARTIAL`,
not NORMAL acceptance. DB0 had zero task009m keys. Details:
`docs/work/handoffs/TD_RABBIT_PHASE_M_AUDIT_20260924.md`. A second isolated DB6
run had the same 210730/604/1/425676/`td_sql=0`/`ack=0` counters and the same
normalized semantic SHA, closing same-input repeat determinism for this cutoff.

Phase N then replayed the same-day real TD window `2026-09-24
09:15:00–09:32:09` through the exact current t1-v2 binary into isolated Redis
DB7/`task009n:`. Independent TD count and t1-v2 `source_in/ticks` were both
429392; the run produced 753 batches, 78 clocks, `td_sql=0`, `ack=0`, and
878448 Redis commands. Core directly read 5222 Q2 hashes at the historical
09:32:09 observation, with coverage=1.0 and 24 stale quotes under the explicit
10-second freshness policy. Ordered/shuffled projection hashes and sampled
Engine hashes matched; the result is `REPLAY_PARTIAL` and
`normal_opening_pass=UNPROVEN`. DB0 had zero `task009n:*` keys. Details:
`docs/work/handoffs/TD_RABBIT_PHASE_N_AUDIT_20260924.md`.

Phase N was repeated into isolated Redis DB8/`task009n2:` with the same
429392 ticks, 753 batches, 78 clocks, 878448 Redis commands, `td_sql=0`, and
`ack=0`. DB7/DB8 normalized semantic Redis content had 5232 keys each,
difference count zero, and identical semantic SHA
`c88639da305a303221c8ea9ca900060a816551bcf0385244aadb62f854050a05` after
excluding runtime metrics. DB0 remained untouched; this closes same-input
repeat determinism for the Phase N isolated bridge, not Rabbit arrival or
historical `available_at` equivalence.

An ECC contract-first audit of the exact t1-v2 Rabbit path found that the wire
schema is known (`DataBatch.batch_id/records/sent_at`, DataRequest compression),
but the C++ runtime `TickBatch` currently retains only
`mode/logical_ts_ms/wall_ts_ms/seq_no/ticks`. Wire `batch_id`, DataBatch
`sent_at`, and `record_count` are not propagated into that runtime batch;
per-tick arrival, delivery sequence, and completion watermark do not exist in
the protobuf contract. Core's RabbitFixtureAdapter remains the canonical
parsed-source contract, while production C++ wiring is a separate future
contract task. Evidence:
`docs/work/handoffs/TD_RABBIT_CONTRACT_AUDIT_20260924.md`.

Phase O then completed a same-day full-window real replay for
`2026-09-24 09:15:00–09:40:00` through the exact current t1-v2 release, with
Redis writes isolated to DB9/`task009o:` and TD writes disabled. Independent TD
count and t1-v2 `source_in/ticks` were both `1,194,572`; the run produced 1,224
batches, 78 clocks, 2,408,279 Redis commands, `td_sql=0`, and `ack=0`. Core
read back 5,222 Q2 hashes with coverage `1.0`, equal ordered/shuffled
projection and sampled Engine hashes, and 116 stale quotes under the explicit
10-second policy. The result remains `PHASE_O_PARTIAL` / `REPLAY_PARTIAL`, not
NORMAL acceptance. Stable 0920/0924/0925 legacy auction projections matched
the prior isolated runs; DB0 had zero `task009o:*` keys. Evidence:
`docs/work/handoffs/TD_RABBIT_PHASE_O_AUDIT_20260924.md` and
`/home/exedev/validation/td-rabbit-phase-o-0924-full-20260924T224351+0800/`.
