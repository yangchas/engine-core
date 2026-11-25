# Phase P full-window real TD → t1-v2 → isolated Redis audit — 2026-09-25

## Outcome

```text
FULL_WINDOW_REPLAY=PASS_WITH_LIMITS
T1_V2_TO_ISOLATED_REDIS=PASS
CORE_READ_ADAPTER=PASS_WITH_LIMITS
BARRIER_AUDIT_REPAIR=PASS
PHASE_P=PARTIAL
TASK_008=PARTIAL_EVIDENCE
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

This tests the intended real path using actual TD rows, one three-second
half-open time slice at a time, t1-v2 replay processing, isolated Redis output,
and Core's read-only projection adapters. It does not claim Rabbit delivery
equivalence or complete Phase P acceptance.

## Real replay

Trade date `2026-09-23`, window `[09:15:00,09:40:00)`, 500 consecutive
three-second slices. The second full run completed:

```text
batches=500
source_in=1,204,178
source_reject=0
ticks=1,204,178
empty=0
ack=0 / ack_fail=0 / ack_skip=0
reject=0 / reject_fail=0
redis_cmds=2,422,292
td_sql=0
redis_committed=1,209,672
```

`td_sql=0` is the write-statement counter; the run did issue 500 bounded TD
`SELECT`s. Replay mode skipped TD writer and did not consume or ACK Rabbit.

## Audit defect found and fixed

The first full-window run exited successfully with the same data counters, but
its barrier CSV grew to 183,630,027 bytes. It contained 218 `0926` summaries;
217 were ordinary `trigger_kind=tick` latest updates and one was the actual
close barrier. Preserve this first file as diagnostic evidence; do not treat
it as valid 0926 barrier auditing.

A self-test regression was added and first failed on that false label. The
fix is local t1-v2 commit `8926cb1a49420c46896f6eec403ca46c85ed6d47` on
`codex/task-q2-pure-function`; full-dependency build and self-test pass. The
fix only allows the `0926` audit tag for the explicit close barrier. A bounded
real TD `[09:26:00,09:26:03)` recheck still emits exactly one
`0926/tick_batch_barrier` summary with five members.

The corrected full replay CSV has exactly these four summary rows:

```text
tag   boundary   trigger             states   candidates
0920  09:20:03   tick_batch_barrier  5,222    4,873
0924  09:24:10   tick_batch_barrier  5,222    5,100
0925  09:25:06   tick_batch_barrier  5,222    5,208
0926  09:26:00   tick_batch_barrier  5,222    5,208
```

Corrected CSV:
`/home/exedev/validation/phasep-t1-v2-redis-full-20260923-run2-20260925T112600Z/auction-barriers.csv`

```text
size:   3,734,857 bytes
lines:  20,893
SHA256: 6765227c09b304bed9d694fb313715e3aef3a31b86410eab6f46d1e479c13581
```

## Isolated Redis write and Core readback

Run 2 wrote to Redis DB 4 under the unique prefix
`phasep3sfull20260925_r26f60ae_run2:`. Before replay, the prefix and fixed
`market:open2m:summary:2026-09-23` target were absent; DB 4 had 5,233 existing
keys, unlimited maxmemory and no evictions. After replay the namespace had
5,233 keys; DB 4 had 10,467 total keys, including one non-namespaced fixed
open2m key from existing t1-v2 behavior. The two services use their default
Redis DB 0; this namespace had zero DB 0 hits. DB 4 reported zero evictions.
No keys were deleted.

Core `RedisQ2ProjectionAdapter` and `read_redis_auction_projection` read the
new namespace without writes, with as-of `2026-09-23T09:40:00+08:00` and a
10-second freshness threshold:

```text
Q2 active/observed: 5,222 / 5,222
Q2 missing:         0
Q2 active coverage: 1.0
Q2 stale:           68
Q2 status:          PARTIAL (mixed freshness)
Q2 content hash:    8b1c6479aad525da8072c55bb4b40ffc9b0241a1a926bc744476905d096704a8
```

Each auction projection was `READY`, `TOP_AMOUNT`, 200 rows:

```text
0920  8aeb253136483a2633fa83ea0ab20b8c67070382f070d26cb5394c05d26b38d4
0924  e4bda4e6b06f0d4c032a477391124e53430e88540656d2012e5a9fdae385622a
0925  6432aecbe66b6ef299309c26cca0cf54cbe8edb03184ce151949327684bcda36
```

The anchor had 5,208 symbols, raw JSON SHA-256
`1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb`.
These Q2, auction and anchor values all equal the first isolated run. The 68
stale records are not missing hashes or parse errors; they make the snapshot
partial at this cutoff. `TOP_AMOUNT` is not full-market coverage.

## Independent retained-baseline comparison

The earlier Redis DB5 baseline `task009k:` was still present at audit time
(5,233 prefixed keys, no evictions). Its active set contained 5,222 symbols;
the sorted-symbol SHA-256 was
`5dfd52d3f75efa6179f463422285c1e1254c7c124a086cbd51dd7f4d49802cb5`. The
current full-window run's active set has the same 5,222 symbols and same
sorted-symbol SHA-256. Core read-only adapter comparison also found exact
content-hash equality for each retained frozen `TOP_AMOUNT` projection:

```text
0920  8aeb253136483a2633fa83ea0ab20b8c67070382f070d26cb5394c05d26b38d4
0924  e4bda4e6b06f0d4c032a477391124e53430e88540656d2012e5a9fdae385622a
0925  6432aecbe66b6ef299309c26cca0cf54cbe8edb03184ce151949327684bcda36
```

The 5,208-symbol 0925 anchor raw JSON was byte-identical by SHA-256:
`1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb`.
This provides an independent historical comparison for active membership
and frozen auction outputs. The DB5 baseline window ends at 09:25:09, whereas
the new replay ends at 09:40:00; therefore it is not a same-cutoff oracle for
the evolving per-symbol Q2 latest values, and no such Q2 value parity is
claimed here.

## Phase alignment / limitations

This demonstrates a bounded current-source path over one full real window:
time-sliced TD read → t1-v2 → isolated Redis → Core read adapters. It does not
prove:

- TD slice membership equals Rabbit `DataBatch` membership/boundaries;
- Rabbit delivery/member/arrival order or live/replay scheduling equivalence;
- historical `available_at` or what Redis exposed at the original 09:25:06;
- full-market completeness of TopN projections;
- 10-second freshness for every Q2 record;
- TD write health, NORMAL acceptance, or M3-1 readiness.

```text
Rabbit delivery/member/arrival order = UNKNOWN
historical available_at             = UNKNOWN
Q2 freshness at 10s                 = PARTIAL (68 stale)
auction scope                       = TOP_AMOUNT (200 each)
production TD writes                = NONE
production Redis DB0 writes         = NONE
Rabbit consume/ACK                  = NONE
systemd/service changes             = NONE
push/merge/deployment               = NONE
```

Validation locations:

- Corrected run:
  `/home/exedev/validation/phasep-t1-v2-redis-full-20260923-run2-20260925T112600Z/`
- First run (including preserved invalid audit CSV):
  `/home/exedev/validation/phasep-t1-v2-redis-full-20260923-20260925T111500Z/`
- Bounded real 09:26 regression:
  `/home/exedev/validation/t1v2-one-batch-repair-20260925/real-0926-audit-regression/`
- Full t1-v2 build/self-test binary:
  `/home/exedev/validation/t1v2-one-batch-repair-20260925/t1_v2_latest_audit_green`

Core source was unchanged in this follow-up; only evidence documentation was
added. Do not start strategy/migration work from this result. Keep TASK-008 /
Phase P `PARTIAL` until remaining real-source and timing evidence is independently
closed.
