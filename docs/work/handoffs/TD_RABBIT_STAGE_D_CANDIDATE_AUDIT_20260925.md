# Stage D — fifth-depth candidate repair and audit (2026-09-25)

## Result

```text
IMPLEMENTATION=PASS
REAL_TD_REPLAY=PASS
CANDIDATE_SELECTION=PASS_WITH_LIMITS
STAGE_D=PARTIAL
PHASE_P=PARTIAL
TASK_008=PARTIAL
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

This closes the bounded fifth-depth candidate-selector repair and its audit for
the two queried historical dates. It does not authorize Phase E work or promote
TASK-008. Prior Phase E evidence remains separate and is not recertified here.

## Implementation

t1-v2 development commit:

```text
b2a575ca585f248e4454997751af87590aa76bd3
branch: codex/task-q2-pure-function
```

The existing `QuoteState` now holds one compact candidate observation per
symbol and inferred local trade date. The same `EngineCore::on_batch` rule is
used in live and replay modes:

- truncate source timestamps to whole local seconds;
- accept a fifth-depth candidate in `[09:24:57,09:25:07)` when either fifth
  bid or fifth ask price is positive;
- keep the candidate with the greatest source timestamp already processed;
- on conflicting book contents at that exact timestamp, retain the ambiguity
  flag rather than asserting a source order;
- reset candidate state when that symbol first receives a tick from a new local
  date;
- keep every tick flowing through the existing Q2/auction calculations; the
  candidate view does not pre-deduplicate or filter the calculation input.

The replay barrier CSV now names fifth-depth candidates separately from
positive auction-amount states. Candidate rows are included only when their
inferred date equals the barrier date, preventing an untouched prior-day
candidate from being presented as current-day coverage.

## Verification

Full-dependency command:

```text
bash make.sh --full --self-test --out=/tmp/t1v2_stage_d_fifth_depth_trade_date
```

Result: `t1_v2 self-test passed`. New/updated checks cover a same-source-time
conflict, latest in-window candidate selection, later out-of-window ticks still
reaching Q2 without replacing the candidate, live/replay rule sharing, and
cross-date candidate reset. `git diff --check` passed.

The latest binary was run against real TD `stock_tick_v2` in dry-run mode for
both `[09:15:00,09:25:09)` windows. The TD reader issued one source SELECT per
3-second half-open slice; no data was loaded as a whole-day buffer and no
row-count chunking was introduced.

| Trade date | 3-second slices | source rows / t1 ticks | rejected | 09:25 states | fifth-depth candidates | no candidate | shape counts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-09-18 | 203 | 226,254 / 226,254 | 0 | 5,221 | 5,171 | 50 | both 5,159; bid-only 8; ask-only 4 |
| 2026-09-23 | 203 | 212,022 / 212,022 | 0 | 5,222 | 5,068 | 154 | both 5,056; bid-only 6; ask-only 6 |

The full-run candidate tuple hashes (symbol, selected source timestamp, price,
book shape, fifth-level values, and ambiguity flag) are identical to the prior
full-run outputs:

```text
2026-09-18 7957630e9f59942645908d005b990f6e299a59e3af7edf258dd35cab681ba27c
2026-09-23 7b27c6d25210435fbb861e01fba43ccfd90a4eba6b7d5695b4cca8784a1bde0a
```

A second, bounded `[09:24:57,09:25:09)` replay per day reproduced the same
candidate tuple sets. This checks the replay-window cut without claiming
Rabbit delivery ordering.

Read-only `auction_snapshot_v2` comparison:

| Trade date | Snapshot symbols | Candidate symbols present | Non-NULL candidate prices compared | Price matches | Snapshot price NULL among candidates | Snapshot symbols without a candidate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-09-18 | 5,221 | 5,171 | 4,408 | 4,408 | 763 | 50 |
| 2026-09-23 | 5,222 | 5,068 | 5,068 | 5,068 | 0 | 154 |

These are candidate-price comparisons only. The 763 historical NULL prices on
2026-09-18 remain unavailable, not matches or failures. 09-23 raw slices also
showed four symbols whose fifth levels expanded only on later ticks in the
six-second interval; this is why the first 09:25:00 observation is not treated
as final. One-sided candidate shapes coexisted with multiple stored
`limit_state` values (including Normal and Up/Down); this does not establish
the old snapshot writer's limit calculation as ground truth. Fifth depth is
not a limit-state classifier.

The observed one-sided `limit_state` counts were ASK_ONLY `{-1:2, 0:2}` and
BID_ONLY `{0:1, 1:7}` on 09-18; ASK_ONLY `{-1:3, 0:3}` and BID_ONLY `{0:1,
1:5}` on 09-23. These are associations in the historical projection, not a
validation of its writer version or limit semantics. Neither date had a
same-candidate-source-time conflict in the selected candidate set; conflict
handling is exercised by the self-test, not claimed as an observed production
case.

## Side effects and limits

Run flags explicitly included `--dry-run --no-replay-write-redis
--no-replay-write-tdengine`. Both run logs reported `redis=skip`, `ack=0`,
`redis_cmds=0`, and `td_sql=0`; the latter counts writes, while the source
reader still performed SELECT-only TD reads. No Rabbit consumer/ACK was used.
Read-only post-check found `t1-v2-live=active` at PID 304 with `NRestarts=0`,
`engine-next=active` at PID 281775 with `NRestarts=0`, and 21 GB free on `/`.

Still `UNKNOWN`/`UNPROVEN`:

- historical Rabbit arrival order, delivery membership, and `available_at`;
- whether a particular TD event was visible to live processing before the
  09:25:06 wall-clock freeze;
- correctness/provenance of historical `limit_state` values;
- why 763 2026-09-18 snapshot prices are NULL;
- the existing 1,579 raw-versus-derived amount differences;
- coverage for dates/markets beyond the two observed days.

The two dates' `replay_order_status` remains
`TD_ORDER_TS_SYMBOL_TIE_UNSPECIFIED`; it is not a statement about Rabbit
arrival order. Candidate equality does not prove complete source history,
NORMAL acceptance, or full producer/replay equivalence.

Evidence directory:

```text
/home/exedev/validation/t1v2-stage-d-fifth-depth-latest-20260925T134446+0800/
```

The implementation is committed locally only. No push, merge, deployment,
service restart, Phase E operation, or production Redis/TD write was performed.

## Follow-up — TD auction snapshot anchor semantics (2026-09-25)

The t1-v2 development writer had one release-contract mismatch: per-symbol TD
auction rows used the latest `QuoteState.px_milli` and latest change even when
the requested 0920/0924/0925 anchor was unavailable. The writer now uses the
same `auction_snapshot_price_fact` / `auction_change_fact` contract as the
auction projection; unavailable anchor values remain SQL `NULL`. A regression
checks both an anchor that differs from latest quote price and a missing anchor
that must not fall back to latest quote price.

t1-v2 local commit:

```text
344daaee912069a83f31b01d71db79a97b053e50
branch: codex/task-q2-pure-function
not pushed, merged, or deployed
```

Verification: full-dependency build and self-test passed. A fresh real TD
dry-run read 2026-09-18 `[09:15:00,09:25:09)` in 203 three-second slices
(226,254 rows/ticks, zero rejects). It emitted 0920/0924/0925 barrier evidence;
all 5,171 candidate members and their 15 selected state fields exactly match
the prior full-window replay. Redis commands, TD writes, and ACKs were all zero.
The evidence is at
`/home/exedev/validation/t1v2-td-anchor-semantics-20260925T142531+0800/`.

This fixes the development-writer contract, but does not explain the 763 NULL
prices in the stored 2026-09-18 snapshot: current event-time replay has anchor
values for the candidate set, while historical `available_at` and visibility
at the 09:25:06 live freeze remain unknown. The active 2026-09-23 release source
uses the same anchor helper, but the exact binary deployed on 2026-09-18 is not
proven by the available release metadata. Do not infer the cause from this
alignment.

Status remains `CANDIDATE_SELECTION=PASS_WITH_LIMITS`, `STAGE_D=PARTIAL`,
`PHASE_P=PARTIAL`, `TASK_008=PARTIAL`; M3-1 remains blocked and
`TD_WRITE_HEALTH=UNPROVEN`. No next phase is started.
