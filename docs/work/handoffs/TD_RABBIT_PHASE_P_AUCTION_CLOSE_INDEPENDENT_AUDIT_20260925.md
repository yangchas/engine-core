# Independent audit: Phase P auction-close empty-slice fix

Date: 2026-09-25 (Asia/Shanghai)

```text
AUDIT_STATUS=PASS_WITH_LIMITS
AUCTION_CLOSE_BOUNDARY=PASS_WITH_LIMITS
PHASE_P=PARTIAL
TASK_008=PARTIAL_EVIDENCE
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
```

This audit rechecked the committed t1-v2 fix against the existing plan and a
second bounded replay of real 2026-09-23 TD rows. It does not advance Phase P,
TASK-008, or the production gate.

## Version-control state

Audited source commit:

```text
repo: /home/exedev/repos/stock-situation-runtime
branch: codex/task-q2-pure-function
commit: 9472f4cf9fb1c91ed59575be9b0b66433ef738ca
subject: fix(replay): defer empty slices at auction barriers
worktree: clean
remote: not configured
```

Core documentation worktree before this report was clean at `0cfb3fe` and was
16 commits ahead of its configured origin branch. Nothing was pushed or
merged. One detached historical validation worktree,
`/home/exedev/validation/q2-source-abc0fff` at `abc0fff`, has two existing
untracked generated JSON artifacts (`core_repeat_report.json` and
`q2frame_summary.json`). They were left untouched and are not included in this
audit commit because they are unrelated historical evidence, not source edits.

## Contract and code review

The patch matches the current replay contract for this boundary:

- slices remain half-open; an empty `[09:25:57,09:26:00)` slice uses
  `09:25:59.999`, not the excluded right edge;
- 09:26:00 is a business barrier, so rows in that second are grouped before
  the close summary;
- the auction `latest` output is forced at 09:26:00 even when the normal
  one-second throttle would suppress it;
- the change does not alter the frozen 09:20/09:24/09:25 snapshots.

The regression self-test in the fixed binary passed with exit code 0. Its
coverage includes the empty pre-close half-open slice, same-second 09:26 rows,
rows after the barrier, and forced close-summary emission.

Non-blocking implementation note: `TdReplayTickSource::emit_pending_batch()`
updates `emitted_barrier_mask_` for `Clock` segments, then contains another
`segment.kind == Clock` check in the `TickBatch` path after the Clock branch has
already returned. Thus a tick-carried barrier is not recorded in that mask.
With the current monotonically advancing 3-second scheduler, the next frame is
past that barrier and the real bounded run showed no duplicate trigger. This
did not invalidate the observed fix, but the dead condition should be removed
or covered if barrier-mask behavior is changed later.

## Independent real-data replay

Input was the real TD table `market_data1.stock_tick_v2` for
`2026-09-23 [09:15:00,09:26:03)`. The fixed replay binary SHA-256 was
`87a9cff8e926ceba2078261b2db07f1262873e77db5d0ad2a6ef14033d6a421c`.
The invocation used replay mode, explicit local TD `127.0.0.1:6030`, Redis DB15,
unique per-run key prefixes, `REPLAY_WRITE_REDIS=true`, and explicitly disabled
TD writes. It did not use Rabbit consumption or ACK.

The first connection attempt used the binary's default TD host and failed at
source bootstrap (`batches=0`, `ticks=0`, `redis_cmds=0`, `td_sql=0`). No Redis
prefix was populated by that attempt. The endpoint was then set to the already
running local TD listener; no source or production configuration was changed.

The first independent repeat populated DB15 under
`task009audit20260925T065650+0800:`. A follow-up repeat at 07:13 Asia/Shanghai
used a fresh DB15 namespace,
`task009audit20260925T071306+0800:`, and preserved the process result. Its
normalized Redis content was compared read-only with the earlier fixed run under
`task009githcfix20260925T064500:`:

| Output group | Keys | Audit-run aggregate SHA-256 | Fixed-run aggregate SHA-256 |
|---|---:|---|---|
| Q2, including active set | 5,223 | `40f4acc4863d570d7c670123d3299a2bc394db484ed64627b1b114270614e3cb` | same |
| A2 | 4 | `06908d3412732d83b189fe096662f993a67f992633bf0cee48aa9553184e89d7` | same |
| legacy auction/anchor | 5 | `1c676bc92b8e099c1d7550bb811dd4c2600209402dc26f4008e6ab613e728f06` | same |

For the 07:13 repeat, all 5,233 keys existed in both DB15 namespaces; there
were no extra or missing keys. The sole per-key content difference was the `m2:runtime` hash's
`redis_bytes` field. The run prefix was two bytes longer, and the recorded
`redis_bytes` was eight bytes higher across four Redis commands, consistent
with run-specific key length rather than changed market output. All other
runtime fields matched. The same Q2/A2/legacy group hashes also matched the
first independent repeat.

Both active Q2 sets had 5,222 symbols, no missing `am` field, and the same
`am` sum: `13,621,401,036`. The DB0 key count for both candidate prefixes was
zero. Redis reported `rdb_last_bgsave_status=ok` and
`aof_last_write_status=ok`.

The first repeat's command session did not preserve its exit code. The 07:13
repeat closed that evidence gap: the process exited `0` with
`batches=222`, `clocks=1`, `clock_ts_ms=1790126706000`, `source_in=212027`,
`source_reject=0`, `ticks=212027`, `ack=0`, `reject=0`, `redis_cmds=425620`,
`td_sql=0`, and `redis_committed=211937`. The fixed-run Q2 and auction output
hashes matched exactly. These counters apply to the 07:13 repeat; earlier-run
counters remain recorded in
`TD_RABBIT_PHASE_P_AUCTION_CLOSE_EMPTY_SLICE_20260925.md`.

## Side effects and final alignment

- Writes were confined to the new isolated Redis DB15 prefix; matching DB0
  prefix count was zero. No validation keys were deleted.
- The replay invocation disabled TD writes; no Rabbit source/ACK path was used.
- `engine-next` and `t1-v2-live` remained active with `NRestarts=0`.
- Root and `/home/exedev` each had 22 GB available; no service restart,
  deployment, source-data mutation, or TD retention change occurred.
- Historical TD event-time replay still does not prove Rabbit arrival order,
  exact live batch membership, historical `available_at`, or NORMAL
  acceptance.

This is a real-data repeatability and boundary audit with limits, not full
producer equivalence. Keep Phase P and TASK-008 partial; do not start the next
phase or M3-1 from this result.
