# TASK-008 post-reboot Q2 state audit — 2026-09-28

## Conclusion

The host reboot restored service availability, but it did not establish that
the global Redis Q2 hashes contain the newest market observation. At
2026-09-27 20:15:30, the running `t1-v2-live` process reported one recovered
batch with 1,000 source records, 200 rejected records, 800 accepted ticks,
one ACK, `redis_committed=800`, and `td_sql=0`. The batch's reported source
time was `2026-09-24 15:29:39 +08:00`, while processing happened roughly
76h45m52s later.

Read-only Redis checks at 2026-09-28 01:17 found
`q2:active:20260924` with 800 symbols and no members in the inspected
2026-09-27/28 active sets. All 800 hashes had a `ts`; they contained 416
distinct timestamps spanning 2026-09-24 15:00:00 through 15:29:39. At that
check every hash TTL was 68,031 seconds. `q2:600000` was at 15:29:06 and its
auction fields (`a20/a24/a25`, `am/br/ar`) were zero. These observations are
strongly consistent with the post-reboot batch being processed and refreshing
the old-date Q2 projection.

However, there is no before-write Redis snapshot and no raw Rabbit envelope or
stable batch/member identity. Therefore this audit proves a stale-date Q2
write/refresh was observed; it does **not** prove that a newer Q2 value was
overwritten, nor prove that every current Redis value came from that one
batch. Do not claim either stronger conclusion.

## Host and service state (read-only)

At 2026-09-28 01:16 +08:00:

- Host boot time: `2026-09-27 09:06:54 +08:00`.
- `/` and `/home/exedev`: 40G total, 17G used, 21G available, 45% used.
- `engine-next=active`, MainPID `203417`, `NRestarts=0`.
- `t1-v2-live=active`, MainPID `318`, `NRestarts=0`.
- Journal shows `engine-next` stopped and started at 2026-09-28 00:30:01;
  reason is not established by this audit. This was not another host reboot.
- At host startup `t1-v2-live` initially saw Redis `Connection refused`, then
  `LOADING Redis is loading the dataset in memory`; it later reported
  `source_restart ok=yes` at 09:07:09.
- A 01:17 read-only Redis check found 800/800 active-set members had a `ts`,
  416 distinct source times, min/max 15:00:00/15:29:39, identical TTL
  68,031 seconds, and zero symbols in the inspected 09-27/28 active sets.

The `20:15:30` recovery/progress record was:

```text
after_failures=5, batches=1, source_in=1000, source_reject=200,
ack=1, ack_fail=0, reject=0, ticks=800, redis_cmds=1604,
td_sql=0, redis_committed=800, last_ts_ms=1790234979000,
wall_lag_ms=276351952
```

`last_ts_ms=1790234979000` converts to `2026-09-24 15:29:39 +08:00`.
Redis's maximum observed `ts` for the active set matches that source time.
This alignment supports the connection, but cannot replace a raw source batch
capture or a pre-write Redis snapshot.

## Exact running t1-v2 release source audit

The running executable is:

```text
/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/content/bin/t1_v2
SHA-256: 363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56
```

Release provenance says this is base release
`20260909_9fd4a42` plus a one-file patch that suppresses TDengine storage for
local times after 09:45. The patch leaves Redis writes and ACK behavior
untouched. The observed `td_sql=0` for 15:29 data is consistent with that
release contract; it is not evidence that the input was rejected.

Inspection of this exact release's source establishes the following path:

1. `EngineCore::initialize()` constructs a fresh in-memory `QuoteStateStore`;
   this initialization path does not hydrate quote values from Redis.
   `QuoteState` starts with `ts_ms=0` and zero-valued quote fields.
2. A new symbol state sets `auction_fields_reset_pending=true`.
3. `TickDeltaCalculator::apply_tick()` zeros calculated deltas when a tick's
   timestamp is not later than the in-memory state, but
   `QuoteCalculator::apply_base_tick()` still assigns that tick's timestamp,
   price, cumulative amount, and volume into the state. Thus this path does not
   discard an older event-time tick; it can move in-memory quote state
   backwards if older data arrives after newer data in the same process.
4. `RedisV2Writer::should_write_q2()` checks initialization, dirty state,
   anchor dirtiness and process-local write interval. It does not compare the
   incoming state timestamp with the existing Redis hash's `ts`.
5. `build_q2_fields()` serializes `state.ts_ms` and, on a fresh state with
   `auction_fields_reset_pending`, includes auction fields. The formatted
   `HSetMulti` command is executed as Redis `HSET`; it refreshes the Q2 hash
   fields and TTL. The Q2 active-symbol set is updated for the tick's derived
   trade date.

This proves a **possible stale-overwrite mechanism** in the exact deployed
source. It does not prove that the specific Redis keys had newer values before
the 20:15 write, because no pre-write values were captured. The current
zero-valued auction fields are consistent with a fresh process's reset path,
but are not, by themselves, proof that nonzero auction values were erased.

## Timestamp fields: what the deployed consumer actually does

Inspection of the exact release source separates three fields that must not
be conflated:

- `DataRecord.tss` is copied into `SourceTickRecord.tss`, then used as
  `RawTick.ts_ms`. For a live batch, the builder sets `logical_ts_ms` to the
  maximum accepted tick timestamp. Its producer-side origin (exchange/event
  time versus a timestamp assigned upstream) is not documented in the
  inspected schema/consumer sources, so that origin remains `UNKNOWN`.
- `DataBatch.sent_at` exists in `schema.proto`, but the active release's
  protobuf decoder does not read it.
- The consumer parses the message body as a length-prefixed wire envelope
  (`4-byte header length + JSON header + payload`) and copies that inner JSON
  header's `timestamp` into `TickBatch.wall_ts_ms`. It does not read the
  Rabbit AMQP `BasicProperties.timestamp` and does not capture a local receive
  timestamp. The inner wire timestamp's producer-side meaning and unit are
  not established.

The logged `wall_lag_ms` is computed as current system wall clock minus
`last_batch_logical_ts_ms`. It is an age of the batch's latest accepted tick
timestamp at log time, **not** Rabbit queue residence/arrival latency and not
the AMQP property or inner wire-header timestamp. The observed value therefore proves that the
processed tick timestamp was old relative to processing time, but does not
prove when the message was published or delivered. Searches of the checked
`stock-situation-runtime`, the deployed `engine-next` tree, and the t1-v2
systemd working directory `/home/exedev/t1v2work` found the schema and
consumer path, but no corresponding producer assignment/publish path. A
local `git log --all -S'amqp_basic_publish'` search had no matches;
historical `set_tss`/`set_sent_at` hits were in self-tests or generated
protobuf accessors, not a producer. A producer-side source or captured
envelope is still needed to settle the timestamp origin.

The only local HTTP gateway copy is in
`/home/exedev/services/engine-next/backups/rabbitmq-gateway-disabled-20260902/`;
its handler forwards the HTTP request body unchanged and sets the separate
AMQP `BasicProperties.timestamp` to `int(time.time())` (seconds). No copy was
found under the active engine-next tree and no local Rabbit publisher unit
was listed. This is evidence for that archived gateway path only; it says
nothing about the inner wire-header timestamp or `DataRecord.tss` in the
current external publisher path.

At 08:54 +08:00, read-only socket metadata showed PID 318 (`t1_v2`) holding an
established TCP connection to a non-loopback endpoint on port 5672. The local
Docker inventory contained Redis and TDengine only, and the local systemd
service list contained no Rabbit publisher. This places the broker endpoint
outside the inspected local service/container set; it does not identify the
publisher process or its timestamp assignment logic.

## Real-data replay evidence and limits

The candidate base release was separately run in read-only TD mode over real
2026-09-18 data `[09:15:00,09:25:09)` with production Redis/TD writes disabled
and Q2Frame output directed to validation. It processed 226,254 source rows,
with zero source rejects, `ack=0`, and `td_sql=0`; its 09:25:04 state matched
the retained 2026-09-18 Q2Frame projection for the compared fields. This is a
historical candidate-release replay, **not** the exact patched executable
currently running and not proof of Rabbit arrival equivalence. Evidence:

```text
/home/exedev/validation/task008-0925-release-20260909-replay-20260928T003500+0800/
```

The real `auction_snapshot_v2` comparison and its limits remain documented
under:

```text
/home/exedev/validation/task008-0925-snapshot-cohort-audit-20260927/
```

Neither evidence source contains the raw post-reboot Rabbit envelope. The
historical source-time meaning of `DataRecord.tss`, Rabbit arrival order, and
historical `available_at` remain `UNKNOWN`.

## Alignment and next step

### Read-only persistence recheck — 2026-09-28 08:32 +08:00

The previous Redis observation was rechecked at 08:32:18 +08:00 using the
engine-next virtualenv's Redis client (the host does not have `redis-cli`):

- `PING=true`;
- `q2:active:20260924` still has 800 members; the inspected 2026-09-27 and
  2026-09-28 active sets have zero members; `600000` is a member of the
  2026-09-24 set;
- `q2:600000` still exists with `ts=1790234946000`
  (`2026-09-24 15:29:06 +08:00`), auction fields `a20/a24/a25=0`, and TTL
  42,193 seconds;
- `t1-v2-live` remains active at PID 318 with `NRestarts=0`;
- the journal still has no later `t1_v2 progress` or `recovered` record after
  2026-09-27 20:15:30.

With the configured 86,400-second Q2 TTL, the interval from the 20:15:30
recovery batch to this read implies about 42,192 seconds remaining. The
observed 42,193 seconds is consistent (within one second) with that batch
refreshing this key's TTL and no later refresh. This supports, but does not
uniquely prove, that the batch wrote this symbol. The old-date Q2 projection
is still present at this read time; the value immediately before the batch is
unknown, so an overwrite remains unproven.

### New service progress and Redis cohort update — 2026-09-28 09:03 +08:00

A later journal read found a new normal-service progress line at 08:41:04:

```text
batches=108, source_in=93756, source_reject=3608, ack=108, ack_fail=0,
ticks=90148, redis_cmds=3208, td_sql=4, redis_committed=1600,
last_in=1000, last_reject=200, last_ticks=800,
last_ts_ms=1790524800000, wall_lag_ms=31264098
```

`last_ts_ms=1790524800000` is `2026-09-28 00:00:00 +08:00`; the reported
wall lag is consistent with the log time being about 8h41 later. This says
what timestamp the consumer used for the latest batch, not when Rabbit
delivered it or where that timestamp originated. In this release, `batches`,
`source_in`, `source_reject`, `ack`, `ticks`, `redis_cmds`, `td_sql`, and
`redis_committed` are process-cumulative counters; the `last_*` fields describe
the most recent batch. Therefore `td_sql=4` reports four TD statements in the
runtime path since process start, but is not attributable specifically to the
08:41 batch and is not a TD readback or proof of persisted TD state. These are
normal-service counters, not actions initiated by this read-only audit.

The same PID's progress counter changed from `batches=1` at 20:15:30 to
`batches=108` at 08:41:04, proving that 107 more batches were processed
between those progress records. Source inspection of the active release shows
progress logging is triggered by advancement of `last_batch_logical_ts_ms`
relative to `report_interval_seconds`, not by a wall-clock timer. Thus the
absence of an intermediate progress line does not establish consumer idleness.

At 09:00:37, Redis showed `q2:active:20260928=5226` and
`q2:600000.ts=1790524800000`; at 09:03 a sample of 24 symbols in that active
set all had the same `ts`, with nonzero `px`, zero `amt`/`vol`, and phase 0.
This is consistent with a same-date zero-turnover baseline-like cohort, but
the cause is `UNKNOWN`; do not call it malformed or complete based on this
sample. `q2:active:20260924` still had 800 members. No later progress line was
present by the 09:03 check, and PID 318 remained active with `NRestarts=0`.

This newer progress supersedes the earlier statement that there had been no
progress after 20:15:30. It does not resolve the producer timestamp origin,
Rabbit arrival order, or the post-reboot pre-write Q2 values. `TD_WRITE_HEALTH`
and `NORMAL_OPENING_ACCEPTANCE` remain `UNPROVEN`; do not add a timestamp gate
or alter the service from this observation.

No replay algorithm, Q2 semantics, gate, service, or production configuration
was changed. Do not add a strict production rejection gate as a consequence of
this audit. Before proposing a repair, obtain the missing causal evidence if
available: a raw/captured source batch with identity and the Redis values
immediately before its processing. If that evidence is unavailable, the next
safe validation is an isolated, non-production Redis reproduction using
real captured inputs, explicitly labelled as mechanism verification rather
than proof of the historical overwrite. An isolated real-TD mechanism test
using the exact active-release executable has now been completed; details are
recorded below. It strengthens mechanism evidence but does not provide the
missing historical Rabbit causality.

```text
TASK-008=PARTIAL_EVIDENCE
REPLAY_FOR_DEVELOPMENT=USABLE_WITH_LIMITS
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
POST_REBOOT_STATE_AUDIT_ACTIONS=READ_ONLY
LIVE_T1_V2_EFFECT_OBSERVED=REDIS_Q2_WRITE_AND_ACK
LIVE_TD_WRITE_OBSERVED_FOR_2026-09-27_20:15_BATCH=NO (cumulative td_sql=0 at that point)
```

The live service's normal post-reboot processing is an observed production
effect; it was not initiated by the read-only state audit. That initial audit
performed no Redis/TD/Rabbit write, service restart, production-file
modification, task promotion, or commit. The later isolated DB15 replay writes
are documented separately below and were not production writes.

### Read-only host/service follow-up — 2026-09-28 09:09 +08:00

The host boot time was `2026-09-27 09:06:54`; `t1-v2-live` started at
`09:06:57`, consistent with startup immediately after that boot. At 09:09 it
remained active as PID 318 with `NRestarts=0`. `engine-next` was also active
(PID 203417), but the system journal contains a successful
`Stopping` → `Deactivated successfully` → `Stopped` → `Started` sequence at
`2026-09-28 00:30:01`. The available journal excerpt does not identify the
initiator or reason. Do not describe `NRestarts=0` as proof of uninterrupted
service uptime; it does not count a successful stop/start as an automatic
failure restart. No OOM-kill entry was found in the checked kernel journal
window, but this does not establish why the service was stopped.

The latest t1-v2 progress remained 08:41:04 at the 09:09 check. Both services
were active, and `/` remained 45% used with 21G available. No service, Redis,
TD, Rabbit, or production-file mutation was performed by this follow-up.
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

### Latest live progress recheck — 2026-09-28 09:18:20 +08:00

A fresh read-only journal query found consecutive progress records at
09:17:40, 09:17:50, 09:18:00, 09:18:10, and 09:18:20, all from PID 318.
Between the 08:41:04 report (`batches=108`, `td_sql=4`) and 09:18:20, the
process counters advanced to:

```text
batches=538, source_in=224997, source_reject=5578, ack=538, ack_fail=0,
ticks=219419, redis_cmds=134668, td_sql=434, redis_committed=66476,
last_in=162, last_reject=0, last_ticks=162,
last_ts_ms=1790558300000, wall_lag_ms=888
```

`last_ts_ms=1790558300000` converts to `2026-09-28 09:18:20 +08:00`; the
reported 888 ms lag means the accepted tick timestamp was close to host wall
time at that observation. It is not Rabbit receive/arrival latency and does
not identify the producer that assigned the timestamp. `td_sql` is cumulative;
it increased from 4 to 434, but this does not identify which batch emitted
which statement and is not an independent TD readback. The log is strong
evidence that t1-v2 was actively processing ordinary batches during this
window, but not proof of TD persisted-row health or M3-1's cleanup-baseline
gate.

At the same check, `t1-v2-live` remained PID 318 active with `NRestarts=0`,
`engine-next` PID 203417 remained active with `NRestarts=0`, and `/` remained
45% used with 21G available. This does not erase the earlier successful
engine-next stop/start at 00:30:01; its cause remains unknown. No production
service or data was changed by this check. The earlier 09:09 finding that the
latest progress was 08:41 was accurate for that check only and is now
superseded by these newer lines. `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

### Latest progress and runtime check — 2026-09-28 09:23:26 +08:00

The latest journal sample contains progress records through 09:23:00 on PID
318. At 09:23:00, cumulative counters were:

```text
batches=1104, source_in=386713, source_reject=5612, ack=1104, ack_fail=0,
reject=9, ticks=381101, redis_cmds=393140, td_sql=1002, redis_committed=194527,
last_in=196, last_reject=0, last_ticks=196,
last_ts_ms=1790558580000, wall_lag_ms=318
```

The timestamp converts to 09:23:00 +08:00. From 09:18:20 to 09:23:00,
process counters advanced (`batches` 538→1104; `td_sql` 434→1002), and
per-log progress continued at 10-second source-time intervals. This is strong
evidence that the live service was processing current-looking tick timestamps
at the time, but not proof of Rabbit delivery latency, producer timestamp
origin, or TD persisted-row health. The `td_sql` value is cumulative and no
TD SELECT/readback was made. `source_reject=5612` out of `source_in=386713`
(about 1.45%) counts records rejected by `SourceTickBatchBuilder` when
`RawTickConverter` returns false; release source has no per-reason counter.
The inspected converter can reject nonpositive `tss`, malformed six-digit
symbols, non-equity market/symbol pairs, and an SZ index-price heuristic.
The separate `reject=9` is successful Rabbit `basic.reject` calls, not
record-level conversion rejection; `ack_fail=0`, and `last_reject=0` for the
latest batch. The cumulative rejected-record causes are unknown and need
classification before calling the feed clean or broken. A journal filter
from midnight to this check
found no `No enough disk space`, `stage=commit.tdengine`, or `t1_v2 fatal`
matches; that does not replace a TD query or the missing cleanup baseline.

At 09:23:26, both `t1-v2-live` (PID 318) and `engine-next` (PID 203417) were
active with `NRestarts=0`; `/` was 45% used with 21G available. The earlier
`engine-next` stop/start at 00:30:01 remains a separate successful unit event
with unknown initiator/reason. This check did not restart services or access
Redis/TD/Rabbit. Do not infer `TD_WRITE_HEALTH=PROVEN` or promote M3-1 from
these logs alone; `TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain the recorded statuses.

## Exact running-release functional replay check — 2026-09-28

The exact executable used by active `t1-v2-live` was run in explicit TD replay
mode against real `market_data1.stock_tick_v2` data, first for
`2026-09-24 [09:30:00,09:30:03)`, then for the older
`2026-09-23 [09:30:00,09:30:03)`. Each bounded query returned one three-second
slice; the replay source emitted timestamp groups. A dry-run of each date
first confirmed source availability and zero Redis commands.

Redis writes for the functional run were confined to a fresh named prefix in
DB15; all Q2/A2/M2/legacy auction/anchor prefixes were overridden. TD output
was explicitly disabled (`REPLAY_WRITE_TDENGINE=0` and
`--no-replay-write-tdengine`). The real TD source query ran, while runtime
statistics showed `td_sql=0` and `ack=0` for both write invocations. The
isolated prefix had zero keys before; afterward it had 5,042 keys in DB15.
DB0 had zero matching audit-prefix keys. The DB15 test keys remain; no delete
was issued.

Before the older-date run, 4,580 Q2 symbols all had 2026-09-24 `ts`. The
older-date run processed and committed 4,342 Q2 states. The date-specific
active sets shared 3,884 symbols, and every shared symbol's resulting Q2
`ts` was now 2026-09-23; none retained the newer date. Total Q2 hashes were
5,038 afterward (4,342 older date, 696 newer date). For `000001`, the stored
projection changed from 2026-09-24 09:30:00 to 2026-09-23 09:30:00. This is
functional evidence that the exact release executable's replay-to-Redis
path can replace newer-date values with older-date values using real TD rows.

It still does not prove the specific post-reboot Rabbit batch caused the
historical production state. No raw Rabbit envelope/batch identity, Rabbit
arrival order, or pre-write production Q2 snapshot is available. The replay
uses TD event-time ordering and is not live Rabbit equivalence. This does not
justify dropping older same-day event-time ticks or adding a strict timestamp
gate; delayed auction ticks remain a valid possibility. Detailed evidence:

`/home/exedev/validation/task008-live-q2-crossday-replay-20260928T0757+0800/`

After the test, `t1-v2-live` remained PID 318, active, `NRestarts=0`; disk
remained 45% used with 21G available. No production DB0 writes, TD writes,
Rabbit actions, restarts, deployments, source edits, or task-board changes
were performed by this test.

```text
EXACT_RELEASE_FUNCTIONAL_REPLAY=PASS
OLDER_DATE_Q2_OVERWRITE=REPRODUCED_FOR_3884_COMMON_SYMBOLS
POST_REBOOT_RABBIT_CAUSAL_OVERWRITE=UNPROVEN
TASK-008=PARTIAL_EVIDENCE
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

## Development candidate differential — 2026-09-28

The development candidate at `b2aa169` (binary SHA-256
`7f7523d3d60ff8dd1bfb7244149634bd1f8bdb71dc88ca8a4bd67057cdf9555e`) was
tested against the same real TD 3-second slices in fresh DB15 prefixes. After
the 4,580-symbol 2026-09-24 narrow baseline, the 4,342-tick 2026-09-23 slice
reported 3,884 Q2 date-guard skips and 458 commits. The 458 were symbols
without an existing hash in that narrow baseline. The earlier full-day test
established 5,222 current-date hashes, after which all 4,342 prior-date
records were skipped. These are different coverage conditions, not
contradictory results.

A separate same-day reversed-order candidate test used real
2026-09-24 `[09:30:03,09:30:06)` then `[09:30:00,09:30:03)` slices. The guard
skipped zero writes; 4,455 of 5,064 common Q2 timestamps moved backward and
609 remained unchanged. Thus the candidate does not enforce same-day event
time monotonicity. This avoids silently rejecting potentially valid delayed
same-day ticks, but also means no conclusion about actual Rabbit arrival
order or same-day production regressions follows from this test.

The exact active-release binary was then run through the same reversed real
slices in a separate namespace. It reproduced the same counts: 4,455 of
5,064 common Q2 timestamps regressed, 609 were unchanged, and none advanced;
both invocations had `td_sql=0`, `ack=0`. This proves the active binary's
same-day mechanism under the deliberately reversed TD order, but not that
Rabbit used that order in production. The production same-day pre-write
comparison remains missing.

The post-reboot record's source trade date and inspected active Q2 set date
are both 2026-09-24. The cross-date guard is not shown to address that
same-date observation. Without the exact incoming Rabbit batch identity and
the per-symbol Q2 values immediately before its processing, whether a newer
same-day value was overwritten remains `UNPROVEN`. Do not present the
development cross-date guard as a verified fix for this reboot case.

Evidence: `/home/exedev/validation/task008-q2-date-guard-differential-20260928T0815+0800/`.
The test namespaces remain in DB15; no DB0 match, TD write, Rabbit action,
restart, or deployment occurred. `t1-v2-live` remains the old release; the
candidate was not deployed.

## Live source-time, reject, and TD-cutoff follow-up — 2026-09-28 10:33 +08:00

### Current service state and counters

At 10:33:55 +08:00 the host boot time was still 2026-09-27 09:06:54. The
active `t1-v2-live` process was PID 318, started 2026-09-27 09:06:57, with
`NRestarts=0`; its `/proc/318/exe` SHA-256 was
`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`, matching
release `20260923_tdstop0945b`. `engine-next` was active at PID 203417,
`NRestarts=0`, with its last successful start at 00:30:01; that earlier
stop/start's initiator remains unknown. Root filesystem was 44% used with
about 22G available.

The latest sampled progress line (10:33:49) reported:

```text
batches=14233, source_in=10728372, source_reject=394997,
ack=14233, ack_fail=0, reject=42, ticks=10333375,
td_sql=5809, redis_committed=10139710,
last_in=1000, last_reject=0, last_ticks=1000,
last_ts_ms=1790562011000, wall_lag_ms=818708
```

`last_ts_ms` is 10:20:11 +08:00; `wall_lag_ms` is about 13m39 at log time.
At the same follow-up, a read-only Redis check returned `PING=true`,
`q2:active:20260928=5226`, and `q2:000001.ts=1790562018000` (10:20:18).
These facts demonstrate ongoing input processing and Redis state advancement,
and a substantial age of the latest reported source timestamp. NTP was
synchronized at the preceding check. The reported age is computed from host
wall time minus the latest batch logical/tick time; it is **not** an observed
Rabbit queue residence time. The origin of `DataRecord.tss` and the reason for
the growing source-time age remain `UNKNOWN`.

`source_reject/source_in` is about 3.68%. Source inspection confirms
`source_reject` counts individual records for which
`RawTickConverter::from_source_record()` returns false; current progress has
no per-reason split. It is distinct from `reject=42`, which counts
message-level AMQP reject calls. For a successfully decoded partial batch,
accepted records proceed through the pipeline and the Rabbit delivery is ACKed
after successful processing; rejected records are not individually retried.
This makes the missing rejection-reason breakdown important, but does not
establish that the filtered records were valid equities or erroneous loss.
The most recent batch itself had `last_reject=0`.

### TD cutoff was verified, not inferred from the counter alone

The active release's `tdengine_v2_writer.cpp` applies
`td_past_store_cutoff(logical_ts_ms)`. It converts the batch logical/tick time
to local `HHMM` and suppresses TD statements when `HHMM > "0945"`; therefore
09:45:00–09:45:59 are permitted and 09:46:00 onward is not. This is a
source-time cutoff, not a host wall-clock cutoff. The active pipeline passes
the logical timestamp to this writer.

Read-only queries against real `market_data1.stock_tick_v2` confirmed:

```text
[2026-09-28 09:45:00, 09:46:00): 97,621 rows
latest row in that interval: 09:45:59, symbol 600351
[2026-09-28 09:46:00, 09:47:00): 0 rows
```

`td_sql` remained at 5,809 after progress logical time passed 09:46, while
`redis_committed` continued to advance. This matches the explicit deployed
cutoff; it is not evidence that TD failed. A journal filter since 09:45 found
no `No enough disk space`, `stage=commit.tdengine`, or `t1_v2 fatal` match.
This bounded database readback verifies the cutoff behavior and data presence
before it, not full-market completeness. Keep the global/M3-1
`TD_WRITE_HEALTH=UNPROVEN` status: the cleanup baseline and required
controlled-window proof are still missing.

### Timestamp-source audit result

The exact active consumer copies `DataRecord.tss` into `SourceTickRecord.tss`
and then `RawTick.ts_ms`; live batch logical time is derived from accepted
tick timestamps. The schema declares `DataBatch.sent_at`, but the active t1
protobuf decoder does not read it. The inner wire-header `timestamp` is copied
to `wall_ts_ms`; the active service does not read Rabbit
`BasicProperties.timestamp` or capture a local message-receive timestamp.
Therefore this consumer proves which timestamp field it uses, but not whether
the producer assigned `tss` from exchange time, collection time, or send time.

The inspected active `engine-next` unit runs Python
`engine_next.app_main`; the Node `grpc_receiver.js` file in its release tree is
not that systemd entry point (the only local Node process observed was an
unrelated OpenClaw helper). No active source-side `DataRecord.tss` assignment
or publisher path was found in the checked repositories/releases. The archived
disabled gateway remains evidence only about that archived path, not the
current upstream publisher. A raw message envelope or the actual upstream
producer repository is still required to settle `tss`, `sent_at`, and arrival
semantics. No Rabbit consume or queue mutation was performed.

### Updated interpretation

```text
T1_V2_PROCESSING             ACTIVE
REDIS_Q2_ADVANCING           OBSERVED
TD_0945_CUTOFF               CONFIRMED_BY_SOURCE_AND_REAL_READBACK
TD_POST_0945_NO_ROWS         EXPECTED_BY_ACTIVE_RELEASE
SOURCE_TIME_AGE              OBSERVED (~13m39 at 10:33:49)
RABBIT_QUEUE_RESIDENCE       UNKNOWN
SOURCE_REJECT_CAUSES         UNKNOWN
PRODUCER_TSS_ORIGIN          UNKNOWN
PRODUCTION_Q2_OVERWRITE      UNPROVEN
TASK-008                     PARTIAL_EVIDENCE
M3_1_NORMAL                  BLOCKED
TD_WRITE_HEALTH              UNPROVEN
```

Do not interpret the expected post-09:45 TD cutoff as a failure, and do not
turn the source-time age into a hard late-tick rejection gate. Do not change
ACK or producer behavior. The next evidence needed is the actual upstream
producer assignment for `tss`/`sent_at` and a record-level rejection-reason
distribution. No service, code, Redis, TD, Rabbit, or production data was
changed by this audit; all Redis/TD commands here were read-only.

## Live progress refresh — 2026-09-28 10:42 +08:00

At 10:42:23, another journal sample showed continuing progress:

```text
batches=16025, source_in=12192358, source_reject=456000,
ack=16025, ack_fail=0, reject=42, ticks=11736358,
td_sql=5809, redis_committed=11539986,
last_in=724, last_reject=0, last_ticks=724,
last_ts_ms=1790562491000, wall_lag_ms=852004
```

The logical timestamp is 10:28:11 +08:00; its age at logging was 14m12.
`source_reject/source_in` was about 3.74%. Redis `PING` remained true,
`q2:active:20260928` had 5,226 members, and `q2:000001.ts` was 10:28:09,
within two seconds of the reported logical time. At 10:43:15 both services
were active with `NRestarts=0`; root filesystem was 45% used with 21G free.

Between progress observations at 10:33:49 and 10:42:23, logical source time
advanced 8m while wall time advanced 8m34. Thus the measured age grew about
33 seconds during that interval: the process remains behind, though the lag
was not diverging rapidly over this particular sample. This is still only
source-time age. Until upstream `tss` assignment and a send/receive timestamp
are identified, do not call it Rabbit queue latency or use it to reject late
data. `td_sql` remained flat because the exact active release suppresses TD
statements after logical time 09:45; Redis continues to advance by design.
`TASK-008=PARTIAL_EVIDENCE`, `M3_1_NORMAL=BLOCKED`, and
`TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

## Current read-only refresh — 2026-09-28 10:53 +08:00

At 10:53:23, the latest `t1-v2-live` progress sample was:

```text
batches=18328, source_in=14090324, source_reject=537025,
ack=18328, ack_fail=0, reject=42, ticks=13553299,
td_sql=5809, redis_committed=13353836,
last_in=742, last_reject=0, last_ticks=742,
last_ts_ms=1790563121000, wall_lag_ms=882374
```

`last_ts_ms` is 10:38:41 +08:00, about 14m42 behind the log wall time.
Compared with the 10:42:23 sample, source time advanced 10m30 during 11m of
wall time, so measured age grew by about 30 seconds. This is still a
substantial age of the consumer's latest reported tick timestamp, not a
measurement of Rabbit queue residence time. The latest `last_reject=0` does
not negate the cumulative rejection count: `source_reject/source_in` is about
3.81%, and the release has no per-reason counter.

Across 86 progress lines in the preceding 15 minutes, sampled `last_reject`
values were: 0 (38), 1 (2), 18 (20), 177 (6), 178 (11), 179 (7), 40 (1), and
67 (1). This repeated pattern is consistent with recurring cohorts filtered
by a stable conversion rule, but it does not identify which rule or symbols.
Progress lines are periodic latest-batch samples, not a complete batch ledger;
these counts must not be presented as the full distribution of all batches.
They justify prioritizing reason-level evidence, not changing acceptance
rules or producer behavior.

At 10:53:24, host boot time was still 2026-09-27 09:06:54 +08:00; no new host
reboot was observed on this machine today. `t1-v2-live` remained active as PID
318, started 2026-09-27 09:06:57, `NRestarts=0`; `engine-next` remained active
as PID 203417, with its current process start at 2026-09-28 00:30:01 and
`NRestarts=0`. Root filesystem remained 45% used with 21G available. These are
process/host observations, not proof of uninterrupted application health.
`td_sql` remains at 5,809, consistent with the already verified source-time
09:45 cutoff; this refresh did not perform a new TD query. Redis counters in
the progress line advanced, but Redis keys were not independently reread in
this refresh.

No service, application code, Rabbit, Redis, or TD data was changed. Tests were
not run because this was a read-only operational audit. Status remains
`TASK-008=PARTIAL_EVIDENCE`, `NORMAL_OPENING_ACCEPTANCE=UNPROVEN`,
`M3_1_NORMAL=BLOCKED`, `TD_WRITE_HEALTH=UNPROVEN`.

## Live Redis/source alignment and rejection-path audit — 2026-09-28 11:04 +08:00

At 11:04:38, the t1 progress sample reported:

```text
batches=20693, source_in=16013873, source_reject=617820,
ack=20693, ack_fail=0, reject=42, ticks=15396053,
td_sql=5809, redis_committed=15193108,
last_in=625, last_reject=0, last_ticks=625,
last_ts_ms=1790563761000, wall_lag_ms=917629
```

The rejected/input ratio was 3.858%. The source timestamp is 10:49:21 +08:00,
about 15m18 behind the progress log's wall time. A read-only Redis check at
11:04:47 returned `PING=PONG`, `SCARD q2:active:20260928=5226`, and
`q2:000001.ts=1790563767000` (=10:49:27), six seconds ahead of the latest
progress sample. A preceding 11:04:00 read confirmed that both `000001` and
`600000` were members of the active set and their Q2 timestamps were within
single-digit seconds of t1's then-latest progress timestamp. This is live
evidence consistent with Q2 Redis writes following t1's advancing source
stream; it is not proof of full-market membership, source freshness, or the
exclusive writer. The source time itself remains about 15 minutes behind
wall-clock time, and its producer origin is unknown.

The `/proc/318/exe` SHA-256 matched the active release binary at
`releases/20260923_tdstop0945b/content/bin/t1_v2`
(`363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`). The
release-bundle `raw_tick_converter.cpp` matched the checked development source
after normalizing line endings. Its `false` conversion paths are specifically:

- `tss <= 0`, or symbol is not exactly six ASCII digits;
- inferred/source market and symbol prefix fail the converter's equity filter;
- SZ equity symbol beginning with `0`/`3` has `lp >= 1000` and triggers the
  SZ index-price heuristic.

These checks are sequential and collapse to one boolean; no reason-specific
counter is emitted. Non-finite or non-positive prices/amounts are converted
to zero rather than rejected by this converter, so they are not explanations
for `source_reject` in this code path. For a decoded delivery with at least
one accepted record, the batch proceeds and the delivery is ACKed after the
pipeline, even if sibling records were rejected. If every decoded record is
rejected, the source returns `Skipped` with `requeue=false`, and the runtime
rejects the whole delivery. The cumulative `reject=42` is message-level and
does not identify which path produced those rejects; do not equate all 42
with all-record conversion failures.

This narrows the hypothesis space but does not classify the observed 617,820
records. No raw rejected Rabbit records were captured, and reading/consuming
the live queue was not part of this check. No service, code, Redis, TD, or
Rabbit state was changed; tests were not run. Next useful step is a
behavior-preserving reason counter in the development code plus tests, followed
by a separately authorized rollout/readback if live reason counts are needed.
Do not change acceptance, ACK, or timing behavior based on aggregate counters.

## Live timestamp cross-check and reject-diagnostic candidate — 2026-09-28 11:41 +08:00

At 11:41:20, `t1-v2-live` was active as PID 318 with `NRestarts=0`,
`engine-next` was active as PID 203417 with `NRestarts=0`, root remained 45%
used, and host time reported `NTPSynchronized=yes`. A progress line at
11:33:52 had reported:

```text
batches=26828, source_in=20894625, source_reject=843266,
ack=26828, ack_fail=0, reject=43, ticks=20051359,
td_sql=5809, redis_committed=20801468,
last_in=1000, last_reject=0, last_ticks=1000,
last_ts_ms=1790565532000 (=11:18:52), wall_lag_ms=900026 (~15m00)
```

Read-only Redis returned `PING=PONG`, `SCARD q2:active:20260928=5226`,
`q2:000001.ts=1790565534000` (=11:18:54), and
`q2:600000.ts=1790565532000` (=11:18:52). Those two symbol samples were
within two seconds of that batch's maximum event time; they do not prove
whole-market coverage, a unique writer, or monotonic per-symbol writes.

At 11:41:17, a newer progress line reported:

```text
batches=28260, source_in=21980759, source_reject=896683 (4.0794%),
ack=28260, ack_fail=0, reject=43, ticks=21084076,
td_sql=5809, redis_committed=23744866,
last_in=647, last_reject=0, last_ticks=647,
last_ts_ms=1790565962000 (=11:26:02), wall_lag_ms=915855 (~15m16)
```

At 11:41:20.893, `m2:runtime:20260928` had
`source_ts=1790565964000` (=11:26:04.000), `wall_ts=1790565964215`
(=11:26:04.215), `delay_ms=215`, and this last batch had 861 inputs, 843
accepted, and 18 rejected. Redis still returned `PING=PONG` and active-set
cardinality 5,226; `q2:000001.ts=11:26:03` and `q2:600000.ts=11:26:04`.
The header timestamp was about 15m16.7 behind the synchronized host clock,
while its difference from this batch's maximum tick time was 215 ms.

Read-only `m2:runtime:20260928` at 11:34:49 showed
`source_ts=1790565586000` (=11:19:46.000), `wall_ts=1790565586465`
(=11:19:46.465), and `delay_ms=465`. At 11:35:33 it showed
`source_ts=1790565630000` (=11:20:30.000), `wall_ts=1790565630498`
(=11:20:30.498), and `delay_ms=498`; Q2 timestamps for `000001` and `600000`
were 11:20:30 and 11:20:28. The server observation time was 11:35:33.836, so
the AMQP header timestamp stored as `wall_ts` was about 15m03s behind the
synchronized host clock even though the code's header-minus-tick `delay_ms`
was about 0.5 seconds.

Source audit establishes that `last_ts_ms`/`source_ts` is the maximum accepted
tick event timestamp in that batch, while `wall_ts` is the parsed AMQP header
timestamp. The runtime computes `wall_lag_ms` from host clock minus event time;
it is not Rabbit queue residence. This live sample shows the header timestamp
is not the consumer's observation time. Its producer meaning (publish time,
upstream capture time, or other) remains `UNKNOWN`; no lateness gate should
be inferred. The observed batch values vary: the runtime snapshot had 1000
inputs / 17 rejects at 11:34:49, 1000 / 179 at 11:35:33, and 861 / 18 at
11:41:20. The cumulative rejection ratio rose from about 4.04% to 4.0794%;
these counts still do not identify causes, and sampled latest batches are not
a full distribution. `reject=43` remains a separate message-level counter.

An isolated development-only diagnostic candidate was committed in
`stock-situation-runtime` worktree
`/home/exedev/repos/stock-situation-runtime-reject-diagnostics`:
branch `codex/task-source-reject-diagnostics`, commit `72a13bb`.
`RawTickConverter` now optionally returns one of the existing sequential
rejection reasons (invalid timestamp, invalid symbol, unsupported
market-symbol pair, or SZ index-price heuristic), and the batch builder
counts each reason plus an unclassified fallback. Acceptance decisions and
output ticks are unchanged. Both dependency-light and full-dependency C++
self-tests passed via `bash make.sh --dev-minimal --self-test` and
`bash make.sh --self-test`; binaries were written under `/tmp`, and
`git diff --check` passed. Builds emitted existing warnings in the
protobuf/hiredis/TDengine adapter code; no warning was introduced by the
rejection-classification changes.

Important source-parity correction: the active release's
`raw_tick_converter.cpp/.h` match the pre-change development files, but its
`source_tick_batch_builder.cpp/.h` do not. The release does not track
`min_tick_ts_ms` and initializes the generic batch `wall_ts_ms` from logical
time (the live Rabbit decoder subsequently replaces it with the AMQP header).
The candidate branch has different replay metadata behavior. The deployment's
recorded base commit `9fd4a42` is absent from the local Git object database and
this checkout has no configured Git remote, so commit `72a13bb` is not claimed
to be a drop-in release patch. It is not pushed, deployed, or restarted, and
does not yet surface the reason counters in the active runtime log. No Rabbit
payload was read; no Redis/TD write, code change in the active release, service
restart, or production side effect occurred.

Status remains `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`,
`TD_WRITE_HEALTH=UNPROVEN`. Exact per-reason live counts remain unavailable.

## Lunch-window readback and release-source candidate — 2026-09-28 11:52 +08:00

At 11:52:40, `t1-v2-live` remained active as PID 318 / `NRestarts=0`,
`engine-next` as PID 203417 / `NRestarts=0`. The last progress line was at
11:45:23, with `batches=29064`, `source_in=22560860`,
`source_reject=927365` (4.1105%), `ack=29064`, `ack_fail=0`, `reject=43`,
`ticks=21633495`, `td_sql=5809`, `redis_committed=25376605`,
`last_in=1000`, `last_reject=0`, and `last_ts_ms=1790566202000`
(=11:30:02), `wall_lag_ms=921461` (~15m21 at log time).

At 11:52:40, read-only `m2:runtime:20260928` still showed
`source_ts=11:30:02.000`, `wall_ts=11:30:02.055`, `delay_ms=55`, and
`source_in=1000`, `source_ok=1000`, `source_rej=0`. Redis active-set
cardinality remained 5,226; the sampled `000001` and `600000` Q2 timestamps
were 11:30:00 and 11:30:01. These final observed source/Q2 times are
consistent with the regular 11:30 Shanghai morning-session end. The absence
of newer progress during lunch is not by itself a service failure. The last
progress log was emitted about 15m21 after the batch's event timestamp; this
is a real processing-age observation, but it cannot be assigned specifically
to Rabbit queue residence without source publisher/arrival evidence.

The active t1 source copies `DataRecord.tss` to the tick's event timestamp,
and copies the outer parsed message-header `timestamp` to `wall_ts`; it does
not consume protobuf `DataBatch.sent_at`.
The live `m2` pair shows the outer header about 55ms after the maximum tick in
this batch, while both are about 15m behind the host when the 11:45 progress
line was emitted. A read-only search of available local repos/services found
the decoder/parser but no Rabbit producer assignment site for that header or
`DataBatch.sent_at`. Therefore producer semantics remain `UNKNOWN`; the
header is not the consumer observation clock in these samples. Do not add a
late-tick gate or alter the replay/event-time contract from this observation.

The earlier developer-branch commit `72a13bb` is not treated as a release
patch because its batch builder has different metadata behavior. To close
that parity gap, an isolated copy of the exact deployed release source was
tested at:

```text
/home/exedev/validation/t1-v2-reject-diagnostics-release-copy.xPGtnm/
```

Its `C/` tree was copied from release `20260923_tdstop0945b`; only eight
`C/t1_v2` files in the copy changed. The exact-release-source candidate adds
the same per-reason counters through the progress log, including cumulative
counts for all-rejected/skipped deliveries, without changing conversion,
ACK/reject, Redis, or TD behavior. Full-dependency `bash make.sh --self-test`
passed in the copy; generated protobuf file hashes match the source release.
Details are in its `CANDIDATE_REPORT.md`. Neither this copy nor the candidate
binary was used against live Rabbit/Redis/TD. The active release remains
unchanged; no push, deployment, or restart occurred.

Status remains `TASK-008=PARTIAL_EVIDENCE`,
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`, `M3_1_NORMAL=BLOCKED`,
`TD_WRITE_HEALTH=UNPROVEN`. Per-reason live counts still require an approved
deployment/readback of the tested exact-source candidate.

## Live-session recheck — 2026-09-28 11:59 +08:00

At 11:59:06, both services remained active (`t1-v2-live` PID 318,
`engine-next` PID 203417; each `NRestarts=0`). Root disk remained 45% used,
with 21G available. No new t1 progress line appeared after 11:45:23; that line
reported `batches=29064`, `source_in=22560860`, `source_reject=927365`
(4.1105%), `ack=29064`, `ack_fail=0`, `reject=43`, `ticks=21633495`,
`td_sql=5809`, `redis_committed=25376605`, and source `last_ts_ms` 11:30:02.

Using the existing Python Redis client with read-only commands, PING succeeded,
`q2:active:20260928` was a set of 5,226 symbols, and `m2:runtime:20260928`
still held `source_ts=11:30:02.000`, `wall_ts=11:30:02.055`,
`source_in=1000`, `source_ok=1000`, and `source_rej=0`. The sampled `q2:000001`
and `q2:600000` timestamps were 11:30:00 and 11:30:01. This agrees with the
morning session ending at 11:30 and the lunch pause; it is not evidence of
current 11:59 market input, complete universe coverage, or Rabbit queue state.

The exact-release-source rejection-diagnostics candidate was rebuilt again
from the isolated copy and its full-dependency self-test passed; output was
written under `/tmp/t1_v2_release_reject_diagnostics_20260928T1159`. This is
candidate test evidence only. The live release, Redis and TD were not changed,
and no deploy or restart occurred. Exact live rejection causes remain
`UNKNOWN`; the morning-vs-afternoon resumption is better checked with a later
read-only sample. Per-reason live counts require a separately approved
candidate rollout/restart and subsequent log readback.

## Afternoon live-flow check — 2026-09-28 14:17 +08:00

Read-only system/journal inspection at 14:16:26 showed both services active
(t1 PID 318, engine-next PID 203417; `NRestarts=0`). Progress resumed after the
lunch pause. The t1 counters advanced from the 14:14:25 row
(`batches=44245`, `source_in=34501853`, `source_reject=1492577`, `ack=44245`,
`ack_fail=0`, `last_ts_ms=14:13:41`, `wall_lag_ms=44540`) to the 14:17:22 row
(`batches=44859`, `source_in=35003961`, `source_reject=1513594`, `ack=44859`,
`ack_fail=0`, `last_ts_ms=14:16:21`, `wall_lag_ms=61640`). `td_sql=5809` was
unchanged, consistent with the deployed post-09:45 TD cutoff.

Two low-volume read-only Redis observations corroborated advancing source/Q2
times. At 14:16:46.947, `m2:runtime:20260928` reported source 14:15:49.000,
header 14:15:49.500 (`delay_ms=500`), and batch input/accepted/rejected
962/944/18; sampled Q2 times were 14:15:48 (`000001`) and 14:15:49 (`600000`).
At 14:17:30.934, source advanced to 14:16:27.000, header 14:16:27.167
(`delay_ms=167`), batch counts were 739/583/156, and Q2 samples were
14:16:25/14:16:27. The active set remained 5,226. These are real post-lunch
progress observations, not proof of complete market coverage or historical
Rabbit arrival/residence time. `source_rej` varies between batches and still
has no live reason breakdown; no late-data gate or rejection policy change is
justified from these counts.

The deployed executable hash was rechecked and still matches the recorded
release SHA; the exact-source diagnostic candidate remains isolated. No
service was changed/restarted; no Rabbit access or Redis/TD write occurred.
The candidate's repeat full-dependency self-test passed at 11:59, but this is
not a live rejection-classification test. Continue to report live rejection
causes as `UNKNOWN` and TASK-008 as partial.

A journal scan from 13:00 for non-progress warning/error/reject/fatal lines
returned no matches. A search of the checked-out current service/Core sources
found the progress formatter but no local consumer/parser of its text format.
This means current observed diagnostics are aggregate progress counters; the
absence of matching error lines does not prove that source records were not
rejected, nor does it establish whether an external log consumer depends on
the existing field sequence.

## Rejection-diagnostic candidate log-order hardening — 2026-09-28 14:18 +08:00

Source review found the initial exact-release candidate inserted the new
`reject_*` and `last_reject_*` fields among the existing progress fields. No
parser was found in the checked-out current service/Core code, but an external
consumer could parse positionally. The isolated candidate was changed to
append all diagnostic fields only after the existing `wall_lag_ms` field; the
self-test now verifies the full legacy field order precedes the added fields.
The exact-release-source full-dependency self-test passed again, producing
`/tmp/t1_v2_release_reject_diagnostics_append_only_20260928T1418`. Generated
`schema.pb.cc/.h` hashes match the deployed release copy, and the source copy
still differs from the release in exactly the eight intended
`C/t1_v2` implementation/test files. This closes the local log-order
regression risk, not unknown external consumer compatibility. No deployed
file or service was changed; the candidate remains uncommitted in a validation
copy and not deployed.

## Later afternoon trend and full field-order test — 2026-09-28 14:28 +08:00

At 14:27:59, progress had reached `batches=47075`, `source_in=36811996`,
`source_reject=1592701` (cumulative ~4.327%), `ack=47075`, `ack_fail=0`,
`reject=806`, and `last_ts_ms=14:26:41`; `wall_lag_ms=78337`. Compared with
14:17:22, counters advanced by 2,216 batches/ACKs, 1,808,035 inputs and 79,107
source rejects (~4.375% of inputs in this interval). The event timestamp moved
from 14:16:21 to 14:26:41 over 10m37 of wall time, with reported event age
moving from ~62s to ~78s. This short interval suggests modestly increasing
age, not an abrupt stall; continue observing rather than imposing a late-data
gate or asserting queue residence. `last_reject` varied by batch; causes remain
unclassified.

At 14:28:02.843, read-only `m2` reported source 14:26:44.000 and header
14:26:44.287 (`delay_ms=287`), with the sampled latest batch `1000/1000/0`
(input/accepted/rejected). Sampled Q2 timestamps were 14:26:42 and 14:26:43;
the active set stayed 5,226. This agrees with advancing Redis state and a
roughly 79-second host/source age at observation time; it does not reveal the
producer meaning of the header or identify where the age accumulates.

The VM has no local `rabbitmqctl`, Rabbit systemd service, or running Rabbit
container. No management endpoint was queried and no consumer/payload path was
opened. Thus queue depth/ready/unacked counts are still unavailable from this
host; the observed ~78s is source-to-host age, not proven broker residence.

The candidate's progress-line test now checks the complete legacy field order
then every appended diagnostic field. Full-dependency self-test passed again
to `/tmp/t1_v2_release_reject_diagnostics_full_order_20260928T1428`;
generated protobuf hashes matched the release copy. All additions remain in
the isolated validation tree. No live restart/deploy, Rabbit access, or
Redis/TD write occurred. The only authorized next live-data step in this
iteration is additional read-only observation unless production rollout is
separately approved.
