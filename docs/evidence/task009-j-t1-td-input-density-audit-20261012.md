# TASK-009-J — t1-v2 source input vs retained TD row density

Audit date: 2026-10-12 (Asia/Shanghai)
Result: `DENSITY_DIFFERENCE=OBSERVED`, `CAUSE_ATTRIBUTION=PARTIAL`

## Question

Reconcile the 2026-10-09 live t1-v2 progress counters with rows retained in
`market_data1.stock_tick_v2`, and determine which parts of a real queue replay
can and cannot be reconstructed from the retained TD source. This is a source
audit, not a replay acceptance gate and not a reason to stop Core migration.

## Release and source evidence

- `t1-v2-live` was active as PID `318`, with no recorded restart (`NRestarts=0`)
  and process start `2026-09-27 09:06:57 +08:00` at audit time.
- `/proc/318/exe` and the immutable deployed release binary
  `/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/content/bin/t1_v2`
  had the same SHA-256:
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.
- The log used was `/home/exedev/t1v2work/logs/t1_v2.log`; its SHA-256 observed
  during this audit was
  `6fc4a5d9bfda56b78879fd18e1e93552637056838cdbc8919f47103d3961a8da`.
  The log is live and may append after this hash was measured. The three
  progress records below are copied into this report to freeze the evidence.

```text
2026-10-09 09:15:01 | t1_v2 progress | batches=216777 | source_in=168050115 | source_reject=7360290 | ack=216777 | ack_fail=0 | reject=10147 | ticks=160689825 | redis_cmds=336371285 | td_sql=22825 | redis_committed=167898148 | last_in=788 | last_reject=18 | last_ticks=770 | pipeline_ms=49 | commit_ms=249 | ack_ms=0 | last_ts_ms=1791508500000 | wall_lag_ms=1145
2026-10-09 09:25:01 | t1_v2 progress | batches=218018 | source_in=168427854 | source_reject=7360440 | ack=218018 | ack_fail=0 | reject=10147 | ticks=161067414 | redis_cmds=336950477 | td_sql=24070 | redis_committed=168185182 | last_in=716 | last_reject=0 | last_ticks=716 | pipeline_ms=5 | commit_ms=39 | ack_ms=0 | last_ts_ms=1791509100000 | wall_lag_ms=1059
2026-10-09 09:42:35 | t1_v2 progress | batches=220814 | source_in=170528660 | source_reject=7436585 | ack=220814 | ack_fail=0 | reject=10185 | ticks=163092075 | redis_cmds=341028310 | td_sql=26868 | redis_committed=170220614 | last_in=1000 | last_reject=18 | last_ticks=982 | pipeline_ms=20 | commit_ms=98 | ack_ms=0 | last_ts_ms=1791509971000 | wall_lag_ms=184745
```

The counters are cumulative process counters. The mid/end checkpoints are
nearest progress records, not per-event boundary exports. In particular, the
last record was emitted at wall time 09:42:35 but its latest source/logical
timestamp was 09:39:31, four seconds beyond the prior report's nominal
09:39:27 endpoint. `wall_lag_ms=184745` confirms that wall time must not be
substituted for event time. The log cannot split a batch exactly at an
arbitrary timestamp, so the window attribution below is close and useful for
density comparison, but is not an exact Rabbit-record census at both edges.
The early `wall_lag_ms` observations were 1,145 ms and 1,059 ms; the final
one was 184,745 ms. In this release that field is wall clock minus the last
batch's logical/source-time field. It is an observed processing-age sample,
not Rabbit arrival latency or a per-record latency distribution.

## Live counter deltas

| Approximate event-time interval | batches | `source_in` | `source_reject` | `ticks` (accepted engine ticks) |
|---|---:|---:|---:|---:|
| 09:15:00 checkpoint → 09:25:00 checkpoint | 1,241 | 377,739 | 150 | 377,589 |
| 09:25:00 checkpoint → 09:39:31 checkpoint | 2,796 | 2,100,806 | 76,145 | 2,024,661 |
| Combined checkpoint delta | 4,037 | 2,478,545 | 76,295 | 2,402,250 |

`source_in - source_reject == ticks` for these deltas. This is consistent with
the exact release code: `SourceTickBatchBuilder` counts incoming records,
rejects failed conversions, and pushes accepted `RawTick`s; the engine's
`tick_count` is the resulting batch tick count. These counters describe t1
processing, not rows durably retained in TD.

## Read-only TD observation

The active process's TD endpoint/database settings were used internally for a
read-only `taos` connection; no credential values were printed or stored. The
server reported TDengine `3.3.6.13`. Queries were limited to `DESCRIBE`,
`SHOW CREATE`, and `SELECT COUNT(*)` against `market_data1.stock_tick_v2`.

Observed schema: `ts TIMESTAMP`; `symbol VARCHAR(20)` tag; remaining tick
columns match the deployed t1 writer's stock-tick field list. The database
precision is milliseconds. The stable definition has no composite primary
key; the t1 writer creates one child table per symbol and writes each eligible
tick's `ts_ms` as its timestamp.

Exact SELECT windows and returned row counts:

```sql
SELECT COUNT(*) FROM market_data1.stock_tick_v2
WHERE ts >= "2026-10-09 09:15:00" AND ts < "2026-10-09 09:25:00";
-- 190549

SELECT COUNT(*) FROM market_data1.stock_tick_v2
WHERE ts >= "2026-10-09 09:25:00" AND ts < "2026-10-09 09:39:31";
-- 957375

SELECT COUNT(*) FROM market_data1.stock_tick_v2
WHERE ts >= "2026-10-09 09:15:00" AND ts < "2026-10-09 09:39:27";
-- 1139461
```

The two checkpoint-aligned TD windows sum to `1,147,924` rows. Against those
approximately aligned windows, live `source_in` is about `2.16x` the retained
row count and accepted `ticks` about `2.09x`. By interval, accepted/live to TD
ratios are about `1.98x` and `2.1x` (the second is about `2.115x`). These
ratios establish a real density difference in this observed
process/date/window; they do not identify its cause or imply that every live
input should become one TD row.

## What code explains—and does not explain

Exact deployed release sources inspected under
`/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/content/source/C/t1_v2/`:

- `source_tick_batch_builder.cpp:14-33`: `source_in` counts records entering
  conversion; rejected conversions are counted separately; accepted records
  are appended without symbol/timestamp deduplication.
- `raw_tick_converter.cpp:32-47,66-79` and `tdengine_v2_writer.cpp:313-329`:
  source conversion and TD writing use equivalent equity market/symbol
  predicates; conversion also rejects nonpositive timestamps and malformed
  symbols before they reach the writer. Thus the repeated writer eligibility
  checks are not a supported explanation for a large additional loss after
  conversion (source comparison, not a per-row runtime counter).
- `runtime_batch_stats.h:41-49`, `runtime_loop.cpp:496-504`, and
  `engine_core.cpp:28-30`: source input, conversion rejects, and engine tick
  count are distinct stages/counters.
- `tdengine_v2_writer.cpp:66-94`: the writer skips empty symbols, nonpositive
  timestamps, and symbols failing `is_equity_market_symbol`; it then emits one
  row value for each remaining input tick. It has no explicit row counter in
  the progress summary.
- `runtime_loop.cpp:28-40`: `td_sql` reports the number of SQL statement
  strings, not the number of tick rows accepted by TD.
- `tdengine_v2_writer.cpp:34-45, 73-90`: rows are written to a per-symbol child
  table with `ts` as the timestamp field and symbol as a tag.

Thus the source-level counters alone cannot close the row balance: accepted
engine ticks are not themselves a durable-row count, and multiple
same-symbol records with the same timestamp cannot be retained as distinct
rows by this schema. `td_sql` advanced by 1,245 and 2,798 across the two
windows, versus 1,241 and 2,796 batches. This is consistent with roughly one
or more TD statement strings per batch, but the aggregate counter cannot
establish that each batch wrote a stock row or that all generated statements
committed. The live evidence does not reveal how many records hit each storage
path. No raw Rabbit payload/batch capture for this interval was found, so
same-key collision multiplicity, delivery membership/order, and exact
per-tick arrival time remain `UNKNOWN`.

The TD database definition observed here did not expose a duplicate-update
option, so this audit does not claim whether a collision preserved the first
or last value. TDengine documents timestamp as the primary key and notes that
multiple rows sharing a timestamp in one insert can overwrite; the installed
server's exact collision winner was not independently tested because that
would require a write.

Reference: [TDengine supertable primary-key rules](https://docs.tdengine.com/tdengine-sql/ddl/stable/)
and [TDengine insert timestamp behavior](https://docs.tdengine.com/tdengine-sql/data-write/insert/).

## Replay implications

The 2026-10-09 TD→deployed-t1-v2 replay is real and deterministic, but it is a
replay of retained TD rows, not a byte/event-exact Rabbit replay. Existing
Task009-F comparison still supports useful feature-scoped development: same-
timestamp frozen auction anchors and basic state matched in the captured
cohort, while event-level `iv/ia/ln`, extrema, and some market labels differed.
Those differences are consistent with missing/replaced intra-timestamp
records and source-path differences, but do not prove a single cause.

Features not reconstructible from current TD evidence alone:

- original Rabbit message boundaries, batch membership, delivery order, and
  per-record arrival time;
- raw multiplicity/order for records that share one symbol and timestamp;
- any filtered/dropped/rejected source record absent from the persisted table;
- exact event-level deltas/extrema where the existing live-vs-replay comparison
  already differs;
- historical `available_at` and full-market completeness.

TD remains useful for replay-driven development of fields/behaviors whose
retained-row semantics have been checked. It must be labelled
`TD_RETAINED_EVENT_REPLAY`, not presented as exact Rabbit stream reproduction.
Do not block unrelated Core development on the unresolved live-vs-TD ratio.

## Status and side effects

```text
LIVE_T1_COUNTERS=OBSERVED
TD_ROW_COUNTS=OBSERVED_READ_ONLY
LIVE_VS_TD_DENSITY_DIFFERENCE=OBSERVED
CAUSE_ATTRIBUTION=PARTIAL
RABBIT_BATCH_MEMBERSHIP=UNKNOWN
SAME_SYMBOL_TIMESTAMP_COLLISION_COUNT=UNKNOWN
EXACT_PER_EVENT_REPLAY=UNPROVEN
TD_RETAINED_DATA_REPLAY_FOR_DEVELOPMENT=USABLE_WITH_FIELD_LIMITS
REDIS_TD_RABBIT_WRITES=NONE
SERVICE_RESTART_OR_MODIFICATION=NONE
```

No Core or runtime source code was modified during the audit. Runtime repo
working tree remains untouched. This finding adds no production gate.
