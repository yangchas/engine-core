# Current Task

## 主线对齐 — 回放用于开发（2026-09-25）

回放的工程目标是复现真实 tick 经 t1-v2 产生 Q2、再由 Core 消费的计算
路径，支持由证据覆盖的事实和策略迭代。每个功能按其实际依赖的输入字段、
单位、版本和时间语义单独核对；未验证的字段不能作为 parity/正确性依据，
但不阻断不依赖它们的开发。历史 Redis `available_at`、Rabbit arrival 和墙钟
可见性未知时，只限制相应实时等价结论。

已有真实 2026-09-24 `[09:15:00,09:32:09)` 证据：精确 t1-v2 release 处理
429,392 行，分别写入隔离 Redis DB7/DB8；Core 读回 5,222 个 Q2，重复运行
语义 key 无差异、ordered/shuffled hash 一致，24 个 quote 在 10 秒 freshness
策略下为 stale。结论：`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`；
`TASK-008=PARTIAL_EVIDENCE`、`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`。

已知边界：09-18 Phase F 的价格和涨跌幅可比项分别为 4408/4408、5209/5209
一致；match amount 有 1,576/5,209 差异，rest bid/ask 分别有 1,626/5,209、
1,621/5,209 差异。金额和剩余量相关功能仍需先核对字段语义/版本，不能因
回放链路可重复就宣称这些功能已验证。详见 Phase F 审计。

2026-09-25 直接读取的当前 Redis Q2 hash 不是历史 09:32 快照，相关 cutoff
探针只作为源时间过滤诊断，证据位于
`/home/exedev/validation/task008-real-redis-audit-20260925-qlzb4egc/`。
主线继续使用冻结的真实 TD/t1-v2/Q2 回放基线做开发与差异比较；不重复用
当前全局 Q2 hash 反推历史，也不等待历史 `available_at` 才开发无关功能。
完整口径见 `docs/work/handoffs/TASK-008-REPLAY-DEV-USABILITY-ALIGNMENT-20260925.md`。

## Latest bounded gate repair — replay status dimensions (2026-09-25)

The frozen-Q2 opening shadow report now separates projection determinism from
whether sampled Engine inputs are comparable. Before Engine evaluation, quotes
with missing source time, another trade date, or an event time after the
whole-second-truncated historical observation cutoff are excluded; subseconds
are discarded, and the complete source projection still records its original
quality diagnostics. CLI status codes distinguish mismatch (`2`) from
not-comparable (`3`). Subsecond cutoff cases (`.000/.197/.999`) are treated as
the same second; the following second is excluded. Regression suite: `16`
targeted and `705` total tests passed; compileall and diff-check pass. The real frozen Redis capture
contains 5,219 future-dated quotes relative to 09:32:10; the four sampled
symbols therefore have no eligible quote, so projection determinism is `PASS`
but Engine comparison is `NOT_COMPARABLE`, not a vacuous pass. Historical
`available_at` remains `UNKNOWN`, and `normal_opening_pass` remains `UNPROVEN`.
No production gate or live path changed. TASK-008 / Phase P remain partial;
M3-1 remains blocked. Details and verification:
`docs/work/handoffs/TASK-008-GATE-STATUS-SEPARATION-20260925.md`.

## Bounded Stage D fifth-depth candidate repair and audit (2026-09-25)

t1-v2 development commit `b2a575ca585f248e4454997751af87590aa76bd3` adds one
per-symbol, per-date fifth-depth candidate to the existing `QuoteState`, shared
by live and replay tick processing. The candidate rule uses truncated local
seconds in `[09:24:57,09:25:07)`, accepts either fifth bid or fifth ask, keeps
the greatest processed source timestamp, flags conflicting same-time books,
resets on a new date, and never filters ticks from Q2. Replay audit output
separates this candidate from positive auction-amount state and excludes stale
prior-date candidates.

Latest full-dependency self-test passed. Latest binary completed real TD
dry-run replays for 2026-09-18 and 2026-09-23 `[09:15:00,09:25:09)`: 203
three-second slices each; respectively 226,254 and 212,022 source rows/ticks;
zero rejects, ACKs, Redis commands, or TD write statements. Candidate tuples
match the prior full-run outputs. Read-only snapshot SELECTs matched every
comparable candidate price: 4,408/4,408 on 09-18 (763 snapshot prices remain
NULL) and 5,068/5,068 on 09-23; 50 and 154 symbols respectively had no new
window candidate. Exact audit and limits:
`docs/work/handoffs/TD_RABBIT_STAGE_D_CANDIDATE_AUDIT_20260925.md`.

This is `CANDIDATE_SELECTION=PASS_WITH_LIMITS`, not whole Stage D/Phase P
acceptance. No next phase is started. Rabbit arrival/delivery order,
historical `available_at`, live 09:25:06 visibility, historical `limit_state`
provenance, NULL prices, and amount differences remain unknown/unresolved.
M3-1 remains blocked and `TD_WRITE_HEALTH=UNPROVEN`.

## Latest Stage D follow-up — TD auction anchor writer repair (2026-09-25)

t1-v2 local commit `344daaee912069a83f31b01d71db79a97b053e50` aligns the TD
auction snapshot writer with the existing anchor-fact contract: 0920/0924/0925
rows use their corresponding captured anchor and serialize unavailable anchor
price/change as `NULL`, never the latest quote as a substitute. Full-dependency
self-test passed. A fresh read-only real TD replay of 2026-09-18
`[09:15:00,09:25:09)` completed 203 three-second slices / 226,254 rows with
zero rejects; all 5,171 candidate states and 15 selected fields matched the
previous full-window replay exactly. `redis_cmds=0`, `td_sql=0`, `ack=0`.
Evidence: `/home/exedev/validation/t1v2-td-anchor-semantics-20260925T142531+0800/`.

The 763 historical NULL A25 prices are still unexplained; historical
availability and live visibility at the 09:25:06 freeze are unknown. The
writer fix does not establish which release generated the 2026-09-18 snapshot.
This is a narrow writer-contract repair only: `STAGE_D=PARTIAL`,
`PHASE_P=PARTIAL`, `TASK_008=PARTIAL`; do not advance to a new phase from this
result. Commit is local only; no push/deploy or TD write.

## Latest Phase P full-window isolated Redis replay audit (2026-09-25)

The real 2026-09-23 `[09:15:00,09:40:00)` TD window was replayed twice through
t1-v2 into isolated Redis databases. The corrected second run completed 500
three-second SELECT slices / 500 t1-v2 batches, 1,204,178 ticks, zero source
rejects, zero ACKs and zero TD write statements. Core's read-only adapters
read back Q2 5,222/5,222 with no missing hashes and 68 stale at the explicit
10-second cutoff; 0920/0924/0925 were each 200-row `TOP_AMOUNT` projections.
Q2, all three auction hashes, and the 5,208-symbol anchor raw SHA-256 match
the first isolated run. A separate read-only comparison with retained DB5 /
`task009k:` also found the active symbol set, all three frozen auction hashes,
and anchor raw SHA-256 equal. The DB5 run ends at 09:25:09, so no 09:40 latest
Q2 value parity is claimed.

The first run's 176 MB barrier CSV is preserved as diagnostic evidence but is
invalid for 0926 auditing: ordinary `tick` latest updates were mislabeled as
0926. t1-v2 commit `8926cb1a49420c46896f6eec403ca46c85ed6d47` fixes the audit
predicate and adds a regression; full-dependency self-test passes. The corrected
full run has exactly four summaries (0920/0924/0925/0926), all
`tick_batch_barrier`; the 0926 record has 5,222 states and 5,208 candidates.

This is real TD → t1-v2 → isolated Redis → Core read-adapter evidence, not
Rabbit delivery equivalence or NORMAL acceptance. Q2 remains `PARTIAL` under
the freshness policy; TopN remains TopN. Rabbit delivery/member/arrival order
and historical `available_at` remain unknown. TASK-008 / Phase P remains
`PARTIAL`; M3-1 remains blocked and `TD_WRITE_HEALTH=UNPROVEN`. Full audit:
`docs/work/handoffs/TD_RABBIT_PHASE_P_FULL_REPLAY_ISOLATED_REDIS_AUDIT_20260925.md`.

## Latest bounded repair — one TickBatch per TD slice (2026-09-25)

t1-v2 local development commit `26f60ae0c87d109421b71ce4dae6f4c8e5025e2e`
now returns one `TickBatch` for each TD half-open 3-second SELECT, retaining all
converted rows. A business barrier is represented as an in-batch tick cut and
callback; it does not split the slice into additional source batches, issue
another TD query, or drop rows. Replay ticks use their own event timestamps for
phase transitions. Barrier audit snapshots are captured at the exact cut and
label the 09:26 auction-close barrier as `0926`.

Real 2026-09-23 evidence includes a 906-row `[09:24:09,09:24:12)` slice whose
09:24:10 snapshot held 603 then-observed auction states; an empty
`[09:25:06,09:25:09)` slice preserved one empty Q2Frame and advanced Core's
timeline without inventing market coverage; and a `[09:26:00,09:26:03)` SELECT
returned 5 rows in one frame, with a `0926/tick_batch_barrier` audit containing
5 states. Evidence is under
`/home/exedev/validation/t1v2-one-batch-repair-20260925/`.

t1-v2 full-dependency build/self-test passed. Core local commit
`5cc45ac4976821ceebf839234c3f9439318ce9f7` accepts genuine empty Q2Frame time
frames while reporting coverage as unknown when no symbol universe is
available; Core verification passed (`693 passed`, compileall and diff-check).
Both repositories are clean after local commits. No production Redis write,
TD write, Rabbit consume/ACK, service change, push, merge, or deployment was
performed. These commits are local and have not been pushed.

This closes the bounded one-batch-per-slice/barrier-audit repair only. It does
not prove Rabbit delivery membership/order, historical `available_at`, or
live/replay batch equivalence; isolated production-Redis execution and the
broader Phase P comparisons remain open. Keep Phase P/TASK-008 `PARTIAL`; do
not promote a task or start strategy work. Audit:
`docs/work/handoffs/TD_RABBIT_ONE_BATCH_SLICE_REPAIR_20260925.md`.

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

## TASK-008 Phase 3/4 — real same-symbol-per-slice experiment (2026-09-26)

The opt-in replay-only latest-per-symbol policy was tested against the default
all-row path using real TD `stock_tick_v2` for 2026-09-18 09:15–09:40. Both
same-build runs read 500 three-second slices / 1,224,811 rows. The variant
removed 2,728 rows, had no tied max-time groups, and changed 14 final raw Q2
symbol hashes (`mn`/`mx`); 09:20/09:24/09:25:06/09:26 barrier Q2 snapshots also
differed. Core's 09:20 `TOP_AMOUNT` auction summary changed; 09:24/09:25
TOP_AMOUNT output matched for this date/window. Core's final canonical Q2 hash
was equal only because raw-only `mn`/`mx` are excluded from that canonical
mapping. This is not full Redis Q2 parity.

Decision: preserve every returned event through t1-v2 by default. Do not make
pre-processing de-duplication a default or equate a TD time slice with a Rabbit
DataBatch. The experiment policy was committed as `fd6856e` and reverted as
`0aff8cf` after the equivalence hypothesis was rejected; both commits remain in
local history and neither was pushed or deployed. The post-revert full-
dependency build and t1-v2 self-test passed. The Core suite passed `705`, and
compileall and diff-check passed. The all-row repeat to Redis DB10/DB11 matched
Q2/A2/legacy auction/anchor content except M2 runtime `redis_bytes`. Both Core
barrier Q2Frame replays were deterministic. Redis output was isolated to
DB6/DB10/DB11, DB0 run-prefix hits were zero, TD writes and ACKs were zero, and
both services remained active with no restarts.

Evidence and limits:
`docs/work/handoffs/TASK-008-PHASE3-DEDUP-AB-20260926.md` and
`/home/exedev/validation/task008-dedup-20260926T210044+0800/dedup_ab_report.md`.
Current direct replay logs do not contain a per-frame row-count/digest manifest;
an older real SELECT inventory reports 500 frames/98 empty frames but does not
attest each frame in these runs. Rabbit arrival/order and historical
`available_at` remain `UNKNOWN`. `TASK-008=PARTIAL_EVIDENCE`,
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged. No next
business phase was promoted.

The audit follow-up on 2026-09-27 reclassified the experiment as
`EXECUTION=COMPLETE` / `LATEST_ONLY_EQUIVALENCE=REJECTED` and recorded that
per-frame input identity was not proven. The restored t1-v2 branch contains no
latest-per-symbol policy.

Mainline reconciliation on 2026-09-27 found that the event-time 09:25:06
barrier already has bounded real TD/Redis evidence in
`TD_RABBIT_PHASE_P_BARRIER_TRACE_AUDIT_20260925.md`; do not repeat that generic
check. The remaining real-time uncertainty is the source/assignment semantics
of `DataRecord.tss`, `DataBatch.sent_at`, and the body-embedded wire-header
`timestamp`. The active consumer reads the inner length-prefixed wire header,
not Rabbit AMQP `BasicProperties.timestamp`; the only local gateway copy is an
archived disabled backup that stamps that separate AMQP property and forwards
the body unchanged. The available current producer source does not establish
the inner fields' semantics, so arrival-time claims remain unknown and do not
block replay work on independently supported fields.

## Post-reboot Q2 release/candidate differential — 2026-09-28

The exact active `t1-v2-live` release binary was exercised in replay mode with
real TD 3-second slices and isolated Redis DB15 prefixes. It can replace
newer-date Q2 hashes with older-date values: 3,884 shared symbols regressed in
the cross-date mechanism test. Development candidate `b2aa169` skipped those
3,884 older-date writes after the same narrow current-date baseline; 458
symbols absent from that narrow baseline were accepted. After the full-day
5,222-symbol baseline, the same 4,342 prior-date source rows were all skipped.

A separate candidate same-day reverse-order test produced zero date-guard
skips; 4,455/5,064 common Q2 timestamps regressed under deliberately reversed
TD slice order. This is not Rabbit arrival evidence and does not justify a
same-day hard gate. The exact active-release executable was then run through
the same reversed real TD slices in a separate DB15 namespace and produced
the same 4,455/5,064 regressions, with 609 unchanged and zero advanced;
`td_sql=0`, `ack=0`. More importantly, the recorded post-reboot source date and
the inspected Q2 active-set date are both 2026-09-24, so the verified
cross-date guard is not proven to fix that same-day observation. The missing
per-symbol pre-write Q2 values and raw Rabbit batch identity keep production
causality `UNPROVEN`; do not advance or deploy a fix from this evidence alone.

Evidence:
`/home/exedev/validation/task008-live-q2-crossday-replay-20260928T0757+0800/`
and
`/home/exedev/validation/task008-q2-date-guard-differential-20260928T0815+0800/`.
DB15 test prefixes remain isolated; DB0 matches=0, `td_sql=0`, `ack=0`, and
both services stayed active without restarts. `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

## Post-reboot runtime/logging recheck — 2026-09-28

At 09:09 +08:00, read-only status showed host boot at 09-27 09:06:54;
`t1-v2-live` PID 318 began three seconds later and remained active with
`NRestarts=0`. `engine-next` was active (PID 203417), but had a successful
systemd stop/start at 09-28 00:30:01; the journal excerpt does not identify
the reason or actor. Its `NRestarts=0` must not be described as uninterrupted
uptime. Root use was 45%, 21G free.

The latest t1-v2 progress line remained 08:41:04. For this same PID, process
counters rose from `batches=1` at 20:15:30 to `batches=108` at 08:41:04, so
the service processed 107 further batches; the missing intermediate log lines
do not mean it was idle. The release emits progress when source logical time
advances by the configured interval, not on a wall-clock schedule. `td_sql=4`
is a nonzero cumulative process counter for four TD statements, not a count
for the last batch; no TD readback was made, so persisted row state remains
unverified. At 09:03 the Redis active set had 5,226 members, but only
24 Q2 hashes were sampled, insufficient to label the cohort complete or
malformed. `last_ts_ms=1790524800000` means the latest consumed source time
was 2026-09-28 00:00:00 +08:00; it does not identify Rabbit delivery time or
the timestamp's producer. Details: `docs/work/handoffs/TASK-008-POST-REBOOT-Q2-AUDIT-20260928.md`.

Status remains `TASK-008=PARTIAL_EVIDENCE`,
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`,
`TD_WRITE_HEALTH=UNPROVEN`. No code, production data, or service was changed
by this read-only recheck.

## Latest t1-v2 progress — 2026-09-28 09:18:20 +08:00

A fresh journal read found progress records every ten source seconds from
09:17:40 through 09:18:20 on PID 318. At 09:18:20 the cumulative counters
were `batches=538`, `source_in=224997`, `source_reject=5578`, `ack=538`,
`ack_fail=0`, `ticks=219419`, `redis_cmds=134668`, `td_sql=434`, and
`redis_committed=66476`; latest-batch counters were `last_in=162`,
`last_reject=0`, `last_ticks=162`. `last_ts_ms=1790558300000` is
09:18:20 +08:00, with `wall_lag_ms=888`. This shows the live consumer was
processing and its reported source timestamp was close to wall time at this
observation; it does not prove Rabbit arrival latency or producer timestamp
origin. `td_sql` is cumulative, and no TD readback was performed.

This updates the 09:09 snapshot: that earlier check's latest progress was
08:41, but later progress appeared. No production service/data was changed by
this read-only log/status check. Status remains
`TASK-008=PARTIAL_EVIDENCE`, `REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`,
`TD_WRITE_HEALTH=UNPROVEN`.

## Latest read-only service check — 2026-09-28 09:23 +08:00

Progress continued on PID 318 through 09:23:00. Cumulative metrics reached
`batches=1104`, `source_in=386713`, `source_reject=5612`, `ack=1104`,
`ack_fail=0`, `reject=9`, `ticks=381101`, `redis_cmds=393140`, `td_sql=1002`,
`redis_committed=194527`; the latest batch had 196 inputs/ticks and
`last_ts_ms=1790558580000` (= 09:23:00 +08:00), `wall_lag_ms=318`. This is
good evidence of current processing, but `td_sql` is cumulative and not a TD
readback. `source_reject=5612` (about 1.45% of records) is a separate
record-conversion rejection counter: the release increments it when
`RawTickConverter` rejects a source record, but does not report rejection
reasons. The separate `reject=9` counts Rabbit `basic.reject` calls, not
record conversion failures. Latest `last_reject=0`; cumulative causes remain
unknown and are not enough to call the stream either clean or broken. A
midnight-to-now journal filter found no disk-space commit error or fatal
entries. At 09:23:26 both services remained active with `NRestarts=0`,
and disk use remained 45% / 21G free. This strengthens runtime confidence but
does not replace a human-confirmed cleanup baseline or prove persisted TD
health. Do not promote `M3_1_NORMAL` from these logs alone. Handoff:
`docs/work/handoffs/TASK-008-POST-REBOOT-Q2-AUDIT-20260928.md`.

## Live Redis/source alignment and rejection-path audit — 2026-09-28 11:04 +08:00

At 11:04:38, t1 progress was `source_in=16013873`,
`source_reject=617820` (3.858%), `ticks=15396053`, `td_sql=5809`,
`redis_committed=15193108`, `last_ts_ms=1790563761000` (=10:49:21),
`wall_lag_ms=917629` (~15m18). Read-only Redis at 11:04:47 returned PONG,
`q2:active:20260928=5226`, and `q2:000001.ts=1790563767000` (=10:49:27),
six seconds ahead of the progress timestamp. This verifies sampled Q2 state is
advancing near the consumer's source timestamp, not that the data is current
relative to wall clock or that the 5,226-member set is complete.

The active release converter rejects only on nonpositive `tss`, malformed
six-digit symbol, disallowed market/symbol combination, or its SZ
index-price heuristic; reasons are collapsed into one count. All-rejected
deliveries take a non-requeue reject path; partially accepted deliveries are
processed and ACKed. `reject=42` does not distinguish those cases. No raw
rejected Rabbit records were read. The next code step is behavior-preserving
reason counters and tests in development; any rollout/readback needs separate
approval. No service or data state was changed. Status remains
`TASK-008=PARTIAL_EVIDENCE`; M3-1/TD gates remain unproven.

## Latest live progress and TD cutoff confirmation — 2026-09-28 10:33 +08:00

At 10:33:49, the active t1-v2 process (PID 318; release SHA-256
`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`) reported
`batches=14233`, `source_in=10728372`, `source_reject=394997` (about 3.68%),
`ack=14233`, `ack_fail=0`, `reject=42`, `ticks=10333375`, `td_sql=5809`,
`redis_committed=10139710`, `last_reject=0`, and
`last_ts_ms=1790562011000` (= 10:20:11 +08:00) with `wall_lag_ms=818708`.
Redis was reachable read-only, `q2:active:20260928` had 5,226 members, and
`q2:000001.ts` was 10:20:18. `t1-v2-live` and engine-next remained active,
both with `NRestarts=0`; host boot remained 2026-09-27 09:06:54 and `/` was
44% used with 22G available. This demonstrates processing/Redis advancement
and a growing age of reported source time, not Rabbit queue residence latency.

TD writes stop by design in this deployed release after the tick's local
`HHMM` passes 09:45. The writer's guard receives logical tick time; real
read-only TD counts were 97,621 rows in `[09:45,09:46)`, zero in
`[09:46,09:47)`, with the last row at 09:45:59. `td_sql=5809` then remained
flat while `redis_committed` advanced, which matches this cutoff, not evidence
of a TD write error. No matching TD-space/fatal log entry was found since
09:45. `DataRecord.tss` is copied into `RawTick.ts_ms`; the producer assignment
was not found in the checked local code, and the active t1 decoder does not
consume `DataBatch.sent_at`. Therefore timestamp origin and the cause of the
13m39 source-time age remain `UNKNOWN`. `source_reject` has no reason split;
the whole partially accepted message is ACKed after pipeline success, so do
not call these counts harmless or proven data loss without classification.

No service or production data was changed by this audit. Keep
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN`; the latter remains
unproven because M3-1's cleanup-baseline/controlled-window conditions are not
established, not because the intentional post-09:45 TD cutoff failed.
Producer timestamp source and per-reason rejection evidence are the next
unknowns. Detailed record:
`docs/work/handoffs/TASK-008-POST-REBOOT-Q2-AUDIT-20260928.md`.

## Live progress refresh — 2026-09-28 10:42 +08:00

At 10:42:23, `t1-v2-live` reported `batches=16025`,
`source_in=12192358`, `source_reject=456000` (about 3.74%), `ack=16025`,
`ack_fail=0`, `reject=42`, `ticks=11736358`, `td_sql=5809`,
`redis_committed=11539986`, `last_reject=0`, `last_ts_ms=1790562491000`
(10:28:11 +08:00), and `wall_lag_ms=852004` (14m12). Redis remained
reachable, with 5,226 members in `q2:active:20260928`; the sampled
`q2:000001.ts` was 10:28:09, two seconds behind this progress timestamp.
At 10:43:15 both services were active, `NRestarts=0`, and root disk was 45%
used / 21G free.

From the 10:33:49 sample to 10:42:23, the reported source timestamp advanced
8 minutes over 8m34 of wall time; its age grew about 33 seconds and remains
roughly 14 minutes. This is a persistent source-time age, but still cannot be
identified as Rabbit queue residence because producer timestamp origin is
unknown. Do not reject late ticks or add a hard timing gate. The active release's
post-09:45 TD cutoff remains the explanation for the flat `td_sql`; no new TD
write was expected in this later source-time window. TASK-008 remains partial,
NORMAL acceptance unproven, and M3-1 blocked.

## Live progress refresh — 2026-09-28 10:53 +08:00

At 10:53:23, `t1-v2-live` reported `batches=18328`, `source_in=14090324`,
`source_reject=537025` (~3.81%), `ack=18328`, `ack_fail=0`, `reject=42`,
`ticks=13553299`, `td_sql=5809`, `redis_committed=13353836`,
`last_reject=0`, `last_ts_ms=1790563121000` (=10:38:41), and
`wall_lag_ms=882374` (~14m42). In the previous 15 minutes, 86 progress-line
samples showed `last_reject`: 0 (38), 1 (2), 18 (20), 177 (6), 178 (11),
179 (7), 40 (1), 67 (1). These are periodic latest-batch samples, not the
complete batch distribution. Repeated counts suggest a stable cohort but do
not identify its rejection reason; source timestamps still do not prove
Rabbit queue residence. Host boot remained 2026-09-27 09:06:54; t1 PID 318
and engine-next PID 203417 were active with `NRestarts=0`, and root disk was
45% used / 21G free. `td_sql` stayed at 5,809, consistent with the previously
verified post-09:45 source-time cutoff; no TD query or Redis key reread was
performed in this refresh. No timing gate or production behavior change is
justified by these observations. Detailed audit:
`docs/work/handoffs/TASK-008-POST-REBOOT-Q2-AUDIT-20260928.md`.

## Live-session recheck and isolated candidate verification — 2026-09-28 11:59 +08:00

At 11:59:06, `t1-v2-live` (PID 318) and `engine-next` (PID 203417) were both
active with `NRestarts=0`; root filesystem remained 45% used with 21G free.
There was no new t1 progress after 11:45:23. That last progress row reported
`batches=29064`, `source_in=22560860`, `source_reject=927365` (4.1105%),
`ack=29064`, `ack_fail=0`, `reject=43`, `ticks=21633495`, `td_sql=5809`,
`redis_committed=25376605`, and `last_ts_ms=1790566202000` (=11:30:02), with
`wall_lag_ms=921461` (~15m21 at that log time).

Read-only Redis access at 11:59 returned PING and `q2:active:20260928` size
5,226. `m2:runtime:20260928` remained at `source_ts=11:30:02.000`,
`wall_ts=11:30:02.055`, `source_in=1000`, `source_ok=1000`, `source_rej=0`;
sampled Q2 timestamps were 11:30:00 (`000001`) and 11:30:01 (`600000`). This
is consistent with the scheduled morning-session end and lunch pause, not a
fresh 11:59 market-data sample. The active-set size is not proof of full-market
coverage, and no Rabbit consumer/queue was inspected.

The exact deployed-release-source diagnostic candidate was rebuilt and its
full-dependency self-test passed again from the isolated validation copy; the
binary output is under `/tmp/t1_v2_release_reject_diagnostics_20260928T1159`.
This verifies the candidate's deterministic self-test, not its live reason
distribution. No release file, service, Redis data, or TD data was changed;
no deploy or restart occurred. Keep `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`,
`TD_WRITE_HEALTH=UNPROVEN`, and live rejection causes `UNKNOWN`.

## Afternoon live-flow recheck — 2026-09-28 14:17 +08:00

After lunch, progress resumed. From 14:14:25 to 14:17:22, t1 advanced from
`batches=44245` to `44859`, `source_in=34501853` to `35003961`, and
`source_reject=1492577` to `1513594`; ACK advanced by 614 and `ack_fail=0`.
That counter delta is 12,443,101 input records and 586,229 source rejects
(~4.71% for the interval; cumulative ratio ~4.32%). `last_ts_ms` advanced from
14:13:41 to 14:16:21, while `wall_lag_ms` moved from ~45s to ~62s. Services
stayed active with the same PIDs and `NRestarts=0`; `td_sql=5809` remained flat
as expected under the deployed post-09:45 TD cutoff.

Two read-only Redis samples independently confirmed source/Q2 movement:
at 14:16:46, `m2.source_ts=14:15:49`, `wall_ts=14:15:49.500`,
`delay_ms=500`, batch `source_in/ok/rej=962/944/18`, and sampled Q2 timestamps
14:15:48 and 14:15:49; at 14:17:30, source time advanced to 14:16:27, header
to 14:16:27.167 (`delay_ms=167`), batch counts `739/583/156`, and Q2 samples
were 14:16:25 and 14:16:27. The active set remained 5,226. This proves
sampled event/Q2 progress and that header-source delta differs from host-event
lag, but not Rabbit residence time, full-universe coverage, or rejection
causes. A post-13:00 scan found no matching non-progress warning/error lines;
absence of such logs does not prove that source records were not rejected.

## Candidate log compatibility check — 2026-09-28 14:18 +08:00

Review found new reason fields were initially inserted between existing
progress fields. Since a positional external parser could be affected, the
isolated exact-release candidate was hardened to append all new fields after
the existing `wall_lag_ms`; a regression assertion verifies the old field
order. Full-dependency self-test passed to
`/tmp/t1_v2_release_reject_diagnostics_append_only_20260928T1418` and generated
protobuf hashes match the deployed release source. No in-scope checked-out
parser was found, but external consumers remain unverified. Candidate remains
isolated; no live release/service/data changed. Preserve all current partial,
unknown, and blocked statuses; do not add a hard timing gate.

At 14:28, the progress-line regression was strengthened to assert the complete
legacy field sequence followed by all appended reason fields. A new full-
dependency self-test passed to
`/tmp/t1_v2_release_reject_diagnostics_full_order_20260928T1428`; protobuf
hashes again matched the release copy. This is still isolated candidate
verification, not live classification.

## Latest TASK-008 evidence update — 2026-10-02

TASK-008 remains `PARTIAL_EVIDENCE`; normal-opening acceptance is still
`UNPROVEN`. The 2026-09-29 pressure comparison now explicitly distinguishes
pure helper parity from the full production assembly gate. Its repeated report
is `VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE` (8/10 plate denominators differ)
and `production_assembly_gate=NOT_REPLAYED_MISSING_SAME_DATE_REDIS_ANCHOR`.

A same-date, hash-verified 2026-09-30 09:25 capture was then run through the
active release's pure universe classifier. It found 5,210 Redis anchor symbols
and 5,220 TD symbols; all 10 TD-only rows have zero amount/bid/ask but null
`chg_bp`, so the release classifies all 10 as `unknown`. The resulting 5,220
effective TD symbols do not match the 5,210-symbol anchor. The inspected source
would mark `universe_valid=false` and plate facts unavailable if given this
gate result. The full assembler and user-facing 09:26 report were not run or
captured; this is a reproduced conditional gate outcome, not proof of an
observed report suppression. Audit details and limits:
`docs/work/handoffs/TASK-008-OPENING-PLATE-PRICE-FULL-CORE-REPLAY-20261001.md`.

The file-only audit was repeated with identical evidence hashes; 828 tests pass,
compileall and diff-check pass. Production release source and systemd status
were inspected read-only; no production source/service/data was changed and no
live Redis/TD/Rabbit operation, ACK, or effect occurred. Do not make a
second-level timing equality a gate. The real 2026-09-30 09:15–09:25 t1-v2
Q2Frame already exists at
`/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/`;
do not reacquire or invent that period. This is not a full opening dataset. A
current-worktree replay of its pinned Q2Frame has completed at
`/home/exedev/validation/task008-current-core-same-day-20260930-20261002T023500+0800/`;
both runs are deterministic with 603 frames, 199,621 updates, 5,220 symbols,
one Engine, 606 signals, and final hash
`ecbced7e05c5349f544d96d148e72946ba4a08e3502e7d73c831c604fe501eb7`. The input
ends at 09:25:02 and does not support a 09:32 opening replay. The current-Core
`--include-opening` replay of the real 2026-09-29 t1-v2 Q2Frame is now complete:
734 frames / 418,759 updates through 09:32:10, one Engine, deterministic ordered
and repeat passes, final hash
`59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27`. Opening
facts are 5,211 `READY` / 12 `PARTIAL` within the observed 5,223-symbol cohort;
full-market coverage is unproven and NORMAL opening was not evaluated. The
09:25 Core numeric summary matches all 11 compared t1-v2 replay summary values;
the four-second timestamp delta is observed, not a failure gate. The 09:32
archived producer cutoff payload also matches Core on amount, limit state, and
change percentage for all 5,211 shared rows; four source timestamps differ by
3 seconds, and 12 additional stale symbols remain `PARTIAL` in Core rather
than being zeroed. Candidate vs full-cohort denominator differences are
documented in the handoff and machine-readable comparisons at
`/home/exedev/validation/task008-current-core-opening-20260929-20261002T031100+0800/`.
All checksums pass. Preserve the 10 unknown rows from the separate 09-30
production-universe audit as partial evidence rather than silently dropping
them or claiming full-market completeness. M3-1 remains `BLOCKED` and
`TD_WRITE_HEALTH=UNPROVEN`.

## Same-date production assembly and Core partial facts — 2026-10-02

Fresh read-only TD rows for 2026-09-30 were checked against the sealed same-day
capture and fed to the exact active-release pure assembler. The release result
was report-level `PARTIAL`: market overview remained available while plate
facts were unavailable because 5,220 TD 0925 symbols did not match the 5,210
symbol frozen Redis anchor. The 10 TD-only rows have null `chg_bp`; they remain
`UNKNOWN`, not proven inactive. This is a pure assembly result, not a captured
user-facing report.

The Core per-symbol 0924→0925 anchor delta matched the active-release formula
for 5,220 records with zero mismatches. On the 5,964-symbol same-date frozen
mapping, Core emitted pressure facts for 450 plates (149 available, 229
partial, 72 unavailable), retained 813 missing and 2,058 unavailable member
facts, and produced the same content hash on two runs. Full-market coverage
remains unproven. A separate real 2026-09-29 Q2Frame-through-09:32:10 run
already carried same-date TD-derived pressure context into `OPENING_0932`; it
is deterministic and `FACT_ONLY`, not NORMAL acceptance.

The 69 relevant regression tests and diff-check pass. Details and artifact
paths are in
`docs/work/handoffs/TASK-008-SAME-DATE-PRODUCTION-ASSEMBLY-20261002.md`.
No production writes/actions occurred and no Core source was changed in this
continuation. Preserve `TASK-008=PARTIAL_EVIDENCE`,
`REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN`. The next evidence must use same-date full Q2Frame
and auction context; do not mix dates or infer historical availability.

## Current Core replay progress — 2026-10-07

The current Core runner now has an opt-in continuation mode that keeps the
same Engine alive after `OPENING_0932` through the end of the frozen input.
Using the real 2026-09-23 T1 Q2Frame artifacts, ordered and repeat runs each
processed 500 frames / 1,209,672 updates with one Engine and the same final
hash. At the 09:32:10 cutoff, fact, field-status, cross-section, transition,
amount, and limit-summary hashes match the earlier 343-frame run. Snapshot
and strategy-result hashes differ with the longer WindowManager horizon; they
are run-envelope hashes, not the opening fact comparison.

The observed auction anchors are still partial (09:20: 1,319/5,222; 09:24:
3,422/5,222; 09:25: 5,068/5,222 with 154 recovery targets requested). Core did
not apply the available 09:26 sidecar, and source snapshots at 09:32/09:40
were not supplied. The upstream T1 per-frame acquisition manifest is missing.
This is full Core continuation evidence, not all-cutoff parity or NORMAL
acceptance. `832 passed`, compileall and diff-check passed. Details:
`docs/work/handoffs/TASK-008-CORE-FULL-WINDOW-CONTINUATION-20261007.md`.

Keep `TASK-008=PARTIAL_EVIDENCE`, `REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN`. Reconcile the existing 09:26 sidecar and inspect
for same-date 09:32/09:40 producer snapshots before deciding the next narrow
comparison; do not synthesize a missing cutoff from post-run latest state.

## Audit refresh — 2026-10-08

The current-source 2026-09-29 full opening replay is recorded in
`docs/work/handoffs/TASK-008-CURRENT-CODE-FULL-REPLAY-20261008.md`: 734 frames
and 418,759 updates through 09:32:10, one Engine per pass, ordered/repeat
determinism matched, with 5,211 READY and 12 PARTIAL opening facts. The
same-date contexts are pinned; full-market coverage and NORMAL acceptance are
not established.

The current 2026-10-08 Core source has now replayed the same pinned real
2026-09-30 Q2Frame through 09:32:10:
`/home/exedev/validation/task008-0930-current-core-replay-20261008T015810+0800/`.
Its input hash is
`1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a`; the
current ordered/repeat summary matches the archived 2026-10-02 report on all
comparable counts, hashes, and statuses. Each pass used one Engine and
processed 754 frames / 428,586 updates. Opening facts are 5,213 READY and 7
PARTIAL (stale), zero missing, within the 5,220-symbol Q2Frame cohort only;
full-market coverage is unproven. 09:20/09:24 anchors are PENDING; this run
had no barrier Q2Frame sidecar, so producer barrier state cannot be inferred.
09:25 has 5,211 PARTIAL and 9 MISSING.
At the 09:25:06 evaluation, latest included source time was 09:25:02. The
four-second delta is recorded, not treated as failure or turned into a hard
timing gate. Across the full Q2Frame, source-time/frame-time gaps reach 900
seconds; these are per-symbol value ages, not Rabbit arrival latency. Rabbit
arrival/delivery membership and historical `available_at` remain UNKNOWN.
Evidence and cross-run details:
`docs/work/handoffs/TASK-008-CURRENT-SAME-DATE-Q2FRAME-REPLAY-20261008.md`.

The audit-only policy syntax repair rejects malformed auction clock strings
without changing default timing or data-completeness policy; regression and
full-suite verification now pass (`837 passed`), with compileall and
`git diff --check` passing. No live source was queried in this audit. Keep
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN`.

## Recovery anchor zero-sentinel verification — 2026-10-08

Follow-up resolved the field-specific zero/missing concern against pinned
evidence. The archived active t1-v2 release source manifest verifies
`AuctionState` initializes `a20/a24/a25` to zero and its Q2 writer serializes
those values; the exact-release 2026-09-29 Q2Frame has `000001.a25=0` at
09:15, and the Core Q2 adapter maps it to `MISSING/null`. The sealed real
2026-09-30 TD capture has zero `px_milli=0` rows among 15,645 auction rows;
unavailable prices are NULL. Recovery therefore treats zero as missing only
for these three source-defined anchor fields; generic numeric zero remains a
value. The regression is parameterized across all three anchors.

Current verification: `845 passed`, compileall PASS, diff-check PASS; both
source-data manifests pass `sha256sum -c`. No live Wencai response or live
Redis/TD/Rabbit access was used for this follow-up. This verifies the sentinel
mapping, not recovery-provider execution or historical `available_at`.
`TASK-008=PARTIAL_EVIDENCE`; normal opening remains UNPROVEN.

## Same-date field-delta and producer-cutoff reconciliation — 2026-10-08

The field-delta sidecar was independently recomputed from sealed real
2026-09-30 TD auction rows and the same-date frozen mapping, then included with
the existing pressure sidecar in a full Core Q2Frame run. The two-pass report
matches the earlier same-date baseline on the Q2Frame input, 754 frames,
428,586 updates, one Engine, 758 signals, reducer revision, virtual clock,
Engine final-state hash, per-symbol opening facts/status, and pressure summary
hash. The new field-delta summary is deterministic and `FACT_ONLY`; raw-row
parity mismatches are zero.

The 450-plate mapping has 5,964 members, with 5,151 overlapping the captured
5,220-symbol TD cohort, 813 map-only and 69 capture-only symbols. For each of
four fields, 5,146 mapped symbols have both anchor values, five lack a required
anchor value, and 813 have no captured TD row. Plate statuses are 373
available, 67 partial, and 10 unavailable. `full_market_coverage` remains
`UNPROVEN`.

The same-date t1-v2 command capture records a 09:32:10 cutoff publication
with 5,213 rows accepted and no missing/invalid rows. Core has 5,213 READY and
7 PARTIAL facts; this is count agreement, not per-symbol value parity, because
the captured command contains metadata and a payload hash but not payload
rows. Those 7 PARTIAL facts are exactly the symbols whose Q2 source age is
greater than Core's 60-second freshness threshold at the cutoff; the producer
count is consistent with excluding that stale tail, but its symbol membership
cannot be confirmed from metadata alone. No same-date 09:40 producer snapshot
was found in the retained
2026-09-30 validation artifacts. Do not infer either missing cutoff from
post-run `latest` state. Detailed hashes and evidence paths are in
`docs/work/handoffs/TASK-008-PLATE-FIELD-DELTA-Q2FRAME-INTEGRATION-20261008.md`.

Verification after this audit: `854 passed`, compileall PASS, diff-check PASS.
The long full-Q2Frame run used sealed files only and had no observed production
side effects. Keep `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN`.

## 16. Same-date auction anchor timing audit — 2026-10-08

The same-date 2026-09-30 TD auction capture was compared with the pinned
t1-v2 Q2Frame using whole-second timestamps. At 09:20, 925/936 positive TD
prices matched at the TD row's second; at 09:24, 3,163/3,229 matched. Every
positive TD price appeared somewhere in the corresponding candidate window,
and both candidates continued changing within their windows. At 09:25,
5,030/5,030 positive TD prices matched; 190 NULL rows remained missing.

The 09:25:06 timeline observation used source/frame data through 09:25:02.
The first event-time frame at or after the 09:25:30 soft deadline was 09:26:00
(30 seconds later): the full Q2 row state changed, but the 09:25 anchor content
hash and revision stayed unchanged at 5,030 available / 190 missing. This
supports the current policy of retaining source-time differences as evidence,
not as a hard seconds-level rejection, and of not revising an anchor for
unrelated quote updates. It does not prove a late arrival or historical live
visibility: Rabbit order and `available_at` remain `UNKNOWN`.

Evidence and input hashes:
`docs/work/handoffs/TASK-008-ANCHOR-TIMING-REAL-DATA-20260930-20261008.md`.
The audit used sealed files only and made no production changes. Keep
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN`; continue only with
feature-scoped work supported by the pinned source cohort.

## 17. Direct Core auction anchors ↔ same-date TD reconciliation — 2026-10-08

Added a reusable file-only audit and compared the latest hash-pinned Core
two-pass report with the same-date TD snapshot for 09:20/09:24/09:25. Results:

- 09:20: 925 equal positive values; 11 both-positive value differences; 260
  Core-positive/TD-NULL rows; 4,014 matching missing rows; 10 Core-only symbols.
- 09:24: 3,163 equal positive values; 66 both-positive value differences; 80
  Core-positive/TD-NULL rows; 1,906 matching missing rows; 5 Core-only symbols.
- 09:25: exact 5,030/5,030 positive-price parity and 190/190 missing parity
  over identical 5,220-symbol cohorts; no value or availability difference.

The earlier anchors' differences are observations, not a gate on 09:25; their
cause is not established. Core's 09:25 evaluation was at 09:25:06 after source
frames through 09:25:02. The 4-second interval and 4.026–6.026-second
available-row timestamp deltas are context only; timestamp semantics are not
proven equivalent and no Rabbit arrival latency is inferred.

Artifacts:

- runner: `examples/audit_task008_core_anchor_td_snapshot.py`
- tests: `tests/test_task008_core_anchor_td_snapshot.py` (5 passed)
- evidence: `/home/exedev/validation/task008-core-anchor-td-snapshot-all-tags-20260930-20261008T064902+0800/`
- full suite: 869 passed; compileall, diff-check, artifact checksums pass

This is feature-scoped real-source evidence, not NORMAL acceptance, full-market
coverage, Rabbit arrival parity, or historical `available_at`. No live
source/service access or production side effects occurred. Keep
`TASK-008=PARTIAL_EVIDENCE` and continue the migration without promoting these
diagnostic differences to timing gates.

## 18. Missing auction-anchor status correction — 2026-10-08

The opening-transition summary previously labeled an absent 09:25 anchor row
`MISSING`, although absence alone does not prove the producer explicitly
observed and declared the field missing. It now preserves explicit source
`MISSING` and classifies an absent record as `UNKNOWN`; the affected symbol
remains in the facts as unavailable, while other symbols continue processing.
No run-level gate was added.

The regression distinguishes explicit `MISSING`, an opening row without an
anchor record, and an expected symbol absent from both input maps. Full Core
verification passed: `893 passed`, compileall PASS, diff-check PASS. A fresh
calculation using the pinned real 2026-09-30 Q2Frame and report reproduced the
transition and per-symbol fact hashes. That real cohort had 5,220 explicit
anchor rows (5,030 `AVAILABLE`, 190 `MISSING`) and zero absent anchor rows, so
the correction does not change its values/counts; it fixes only the absent-row
case. Evidence details are in
`docs/work/handoffs/TASK-008-COHORT-SOFT-FAILURE-20261008.md`.

This is a data-quality interpretation correction, not a new acceptance gate.
TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening acceptance remains
`UNPROVEN`. The audit used sealed local artifacts only and had no production
side effects.

## 19. Plate field-delta cohort soft-failure — 2026-10-08

The fact-only plate field-delta summary previously validated every supplied
symbol row before applying the selected plate cohort. A malformed row for an
unselected/out-of-scope symbol, or one wrong-anchor row for a selected symbol,
could abort otherwise usable aggregation. It now filters to the selected
cohort first, reports out-of-scope symbols, and marks a malformed selected row
`INVALID` for that symbol's fields without aggregating its values or stopping
other symbols. Date, mapping, and outer input-contract errors remain explicit
errors.

Focused regressions cover wrong-anchor, malformed out-of-scope, and
non-mapping out-of-scope rows plus context validation. Against the pinned real
2026-09-29 TD capture and frozen plate mapping, all four independent raw-row
parities remain `PASS` with zero mismatches; the 68 capture-only symbols are
reported out of scope. All aggregate counts and sums match the prior audit;
the summary hash changes because the out-of-scope diagnostics are now part of
the summary. Full suite: 894 passed; compileall and diff-check pass. Evidence:
`docs/work/handoffs/TASK-008-COHORT-SOFT-FAILURE-20261008.md`.

This is not live-source verification or NORMAL acceptance. TASK-008 remains
`PARTIAL_EVIDENCE`; the audit used sealed artifacts only and had no production
side effects.

## 20. Auction-pressure cohort soft-failure — 2026-10-08

The related fact-only auction-pressure summary had the same pre-scope
validation problem: one non-mapping fact anywhere in the input could stop a
selected-plate report. It now scopes rows before payload validation, counts a
malformed selected symbol as `INVALID`, and reports out-of-scope symbols while
continuing usable members. Summary/context validators preserve and verify the
diagnostics.

Regression tests cover an invalid selected fact and malformed unselected rows.
A hash-pinned audit over the real 2026-09-29 TD capture and frozen mapping
found strict per-symbol helper formula parity for 5,223/5,223 symbols; plate
pressure values and statuses had zero mismatches. Eight plate denominators
differ because 91 selected mapping members are absent from captured TD rows,
so the result remains `VALUE_PARITY_WITH_DENOMINATOR_DIFFERENCE`, not strict
production parity. The selected-cohort summary reports 3,902 facts as outside
that cohort. Evidence:
`/home/exedev/validation/task008-auction-pressure-row-robustness-20261008T115319+0800/`.

Full suite: 895 passed; compileall and diff-check pass. The audit used sealed
files and hash-pinned release source only; Redis/TD/Rabbit and services were
not connected. TASK-008 remains `PARTIAL_EVIDENCE`, and NORMAL opening
acceptance remains `UNPROVEN`.

## 21. 0925 recovery with unknown symbol universe — 2026-10-08

A targeted contract audit reproduced a real recovery dead-end: when the first
09:25 cohort had no rows and no declared expected-symbol universe, Core
generated a full-cohort recovery plan (`requested_symbols=()`, anchor field
requested), but rejected the returned symbols. The revision invariant also
treated an unknown denominator as an empty universe. The scoped fix now allows
the APPLIED result's declared fills and requested anchor field, records the
returned cohort while keeping state `PARTIAL`/`FACT_ONLY` and coverage unknown,
and avoids issuing another automatic full-cohort request when no residual
universe can be calculated. Known-universe requests keep their existing
symbol/field restrictions.

The regression takes its anchor value from the pinned real fixture
`tests/fixtures/q2/q2frame_0925_real_limit_states_20260930.json` (SHA-256
`10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0`); it
does not use a fabricated market value. Verification: `910 passed`,
compileall PASS, `git diff --check` PASS. This is an offline contract check,
not live Wencai/Redis/TD integration. The full Q2Frame replay recorded above
predates this source change and did not exercise recovery-result application.
TASK-008 remains `PARTIAL_EVIDENCE`; no NORMAL or production status is changed.

## 22. Recovery duplicate and opening-row isolation — 2026-10-08

Local robustness defects were reproduced and repaired. Conflicting recovery
rows that normalize to the same symbol are now quarantined for that symbol;
identical aliases deduplicate, valid sibling fills still apply, and the prior
primary row is retained for quarantined members.
A zero-valued auction anchor now remains unavailable even when the normalized
primary value is `None`; it no longer rejects valid sibling fills. A new
zero-only member with no primary row is omitted from the applied cohort, while
the existing member remains missing. A declared fill absent from one symbol's
response likewise leaves that member unavailable and retains diagnostics.
A correct embedded `symbol` on row-list recovery input is identity metadata
when the original cohort was keyed by symbol; a mismatched embedded identity
remains a hard input error. A non-mapping per-symbol anchor/opening row now
contributes an `INVALID`/unavailable fact and diagnostic while other symbols
continue.

Targeted recovery/timeline/opening suite: 103 passed. Full Core suite: 920
passed; compileall and diff-check pass. The regression obtains its primary
`None` from the hash-pinned real 2026-09-30 Q2Frame fixture; the injected
recovery response edge is a contract test, not a real Wencai response.
Recalculation from the pinned real 2026-09-30 Q2Frame through 09:32:10
reproduced the existing clean transition
summary exactly (5,030 comparable symbols; summary hash unchanged). This is
sealed historical-artifact verification, not a new live Redis/TD/Rabbit test.
No producer, Engine execution gate, or production path changed. TASK-008 stays
`PARTIAL_EVIDENCE`; `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`.

The historical TASK-007 queue note was also clarified: unresolved NORMAL/live
evidence does not block unrelated explicitly requested Core feature work. This
does not auto-start a task or promote TASK-008 acceptance.

## 23. Sparse recovery cohort completion — 2026-10-08

The new sparse-response regression exposed a shape mismatch: timeline recovery
rejected an `APPLIED` response that omitted an unchanged primary symbol or
field, even though those facts were already held at the requested base
revision. Core now restores only those omissions from the saved primary cohort
and records `RECOVERY_SOURCE_SYMBOL_RESTORED` /
`RECOVERY_SOURCE_FIELD_RESTORED`. A returned conflict is handled by the
per-symbol quarantine described in section 24; wrong identity, unrequested
fills, and out-of-scope new facts remain rejected. An unavailable new member
is omitted with a diagnostic. Recovery remains `PARTIAL`/fact-only and
idempotent.

The primary rows came from the SHA-pinned real 2026-09-30 Q2Frame fixture; the
sparse response is a contract test, not a real Wencai result. Targeted
recovery/timeline/opening suite: 105 passed; full suite: 922 passed;
compileall and diff-check pass. No Redis/TD/Rabbit connection, production
service action, or production write occurred. The 35-minute real replay was
not rerun because this change is limited to applying a recovery response;
TASK-008 remains `PARTIAL_EVIDENCE` and NORMAL opening remains `UNPROVEN`.

## 24. Isolate conflicting recovery symbols — 2026-10-08

Audit found that one recovery row rewriting an already-observed value rejected
the whole cohort, including valid sibling fills. Recovery validation now
quarantines the conflicting symbol, restores its exact primary row, and keeps
valid sibling fills. Invalid symbol identity and out-of-plan additions remain
hard errors. If no fill survives quarantine, the timeline records
`recovery_state=ERROR`, retains the missing facts, and continues to expose
recovery as required. Idempotent retry preserves the same diagnostics.

Regression evidence includes a conflicting existing member beside a valid
fill, an all-conflicting response, anchor/source-field conflicts, direct
recovery, sparse omissions, idempotency, and existing identity/scope rejection
tests. Recovery/timeline tests: 55 passed; full suite: 924 passed; compileall
and diff-check pass. The response remains synthetic contract input; no live
Wencai response or Redis/TD/Rabbit interaction was exercised. TASK-008 remains
`PARTIAL_EVIDENCE`; NORMAL opening remains `UNPROVEN`.

## 25. Quarantine isolated out-of-scope recovery data — 2026-10-08

Review of the recovery boundary found that one provider row containing an
unrequested field, one out-of-plan symbol, or one fill omitted from the result's
declared fill set could raise while valid sibling anchor fills were usable.
Plan-bound `apply_recovery_result` now removes those facts at the narrowest
scope, preserves the saved primary value, and records source anomaly codes
(including the affected field/symbol where available). Valid declared sibling
fills continue. If nothing valid remains, it records `ERROR`/`PARTIAL` and
keeps recovery required rather than fabricating success. Idempotent retries
retain the original applied/error outcome and diagnostics. If a later timeline
revision already exists, a retry returns that current revision without adding
another revision; the original recovery result remains in revision history.

At this point, embedded `symbol`/row-key disagreement still failed the whole
recovery call; the following follow-up supersedes that behavior. Plan identity,
trade date, and base/result revision envelope errors remain hard failures. The
recovery response is synthetic contract input; pinned real Q2Frame values
supply only the primary baseline. No Wencai capture, live Redis/TD/Rabbit
access, service action, or production write was performed. This is not provider
integration or NORMAL acceptance evidence. TASK-008 remains
`PARTIAL_EVIDENCE`; `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`.

## 26. Quarantine recovery row identity mismatch — 2026-10-08

A recovery row whose embedded symbol disagrees with its mapping key is now
quarantined at member scope in both plan-bound and direct recovery. Core never
reassigns its value to the embedded symbol; it restores the exact primary row
when known and continues valid sibling fills. If no usable fill survives, the
revision records `ERROR` and leaves recovery required. Plan/date/revision
envelope errors remain hard failures.

Red/green regressions cover both entry points, a bad member beside a valid
sibling, primary-value retention, idempotent plan-bound retry, and a bad-only
response. Full Core suite: 927 passed; compileall and diff-check pass. The
recovery response remains contract input paired with a pinned real Q2Frame
primary baseline, not captured Wencai output. No live source or production
write was exercised. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening
remains `UNPROVEN`.

## 27. Quarantine malformed recovery declarations — 2026-10-08

Audit found that one malformed token in `filled_symbols` or `invalid_symbols`
could still throw during result construction and discard the entire recovery
response. Those tokens are now quarantined into stable anomaly codes. Valid
sibling fills continue; if every requested fill is unusable, the timeline
records `ERROR`, keeps facts `PARTIAL`, and leaves recovery required. A clean
empty `APPLIED` response remains rejected. TDD covered malformed fill and
diagnostic declarations, valid sibling continuation, and invalid-only
recovery against the pinned real 2026-09-30 Q2Frame primary baseline. Recovery
responses remain contract inputs, not captured Wencai output. Focused
recovery/timeline/opening tests: 71 passed; full suite: 931 passed;
compileall and diff-check pass. No live source, service action, or production
write occurred. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening remains
`UNPROVEN`.

## 28. Quarantine malformed expected auction-universe members — 2026-10-08

`build_auction_anchor_revision()` now isolates invalid members of the declared
`expected_symbols` universe instead of throwing away valid observed anchors.
It records `INVALID_EXPECTED_SYMBOL`, withholds anchor/source coverage, and
keeps the revision `PARTIAL` because the expected denominator is uncertain.
The test uses a pinned real 2026-09-30 Q2Frame row with one injected malformed
universe member; it verifies robustness, not live-source behavior. Full suite:
933 passed; compileall and diff-check pass. No live source or production side
effect occurred. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL opening remains
`UNPROVEN`.

## 29. Re-run current Core source on captured real Q2Frame — 2026-10-08

After the recovery and malformed-universe robustness changes, reran the full
same-date 2026-09-30 t1-v2 Q2Frame input plus TD-derived pressure and field-
delta contexts through the current Core runner. The report is byte-identical
to the immediately preceding same-input report (SHA-256
`5330060220fb47998d78030725698289befe3ba36072911febf73c11831c7d0e`): ordered
and repeat passes each process 754 frames / 428,586 updates through 09:32:10,
with 758 signals, reducer revision 754, all determinism checks true, and the
same final state hash. Opening facts remain 5,213 READY / 7 PARTIAL; 09:25
standalone anchors remain 5,030 AVAILABLE / 190 MISSING. This proves replay
non-regression on this captured input, not that recovery edge cases or live
providers were exercised. Runtime was about 35 minutes and is not a gate. No
live source or production effect. TASK-008 remains `PARTIAL_EVIDENCE`; NORMAL
opening remains `UNPROVEN`.

Evidence:
`/home/exedev/validation/task008-current-source-replay-20261008T173904+0800/core_q2frame_report.json`.

## 30. Re-run current Core source through the full 09:15–09:40 window — 2026-10-08

Using the SHA-pinned 2026-09-23 TD→t1-v2 Q2Frame artifact and its producer
barrier sidecar, the current Core runner completed `--include-opening
--continue-through-input` with ordered and repeat passes. All determinism
checks are true. Each pass consumed 500 frames / 1,209,672 updates across 5,222
symbols, including 78 empty frames; `processed_signals=507`,
`reducer_revision=503`, VirtualClock ends at 09:39:59, and both final state
hashes equal
`7dae28bd7e409e4f4196a8aba5cf54386a6a8deecdcdd31abd5be43172317e2d` (also
equal to the prior full-window result). Opening is 5,210 READY / 12 PARTIAL;
09:25 anchors are 5,068 AVAILABLE / 154 MISSING. `FACT_ONLY`; no effects.

The separate Phase L run record reports 1,204,178 TD rows; this Q2Frame
artifact contains 1,209,672 updates. The Q2Frame's upstream artifact-build
identity and linkage to that Phase L run were not preserved. Therefore the
5,494 arithmetic difference is not established as a same-run discrepancy,
and does not imply dropped or extra data or create a completeness gate. This
run did not access live TD/Redis/Rabbit or re-run t1-v2. No
retained 09:32/09:40 producer snapshots means producer parity remains
unproven. Runtime ~26.5 minutes is observational only. TASK-008 remains
`PARTIAL_EVIDENCE`; NORMAL opening remains `UNPROVEN`.

Report:
`/home/exedev/validation/task008-current-source-full-window-20261008T181726+0800/core_full_window_report.json`
(SHA-256 `accf5d294809dcfb1b246c914bcf80fe0b39656b843b4c494e1d06533347c65d`).
