# Phase P follow-up: auction-close empty-slice boundary

Date: 2026-09-25 (Asia/Shanghai)
Status: `AUCTION_CLOSE_BOUNDARY=PASS_WITH_LIMITS`
Overall Phase P: `PARTIAL`

## Scope

This follow-up isolates one real-data discrepancy in the t1-v2 TD replay path.
It does not advance the task to live Rabbit integration, strategy work, or M3-1.

The source fix is committed separately in the t1-v2 repository:

```text
repo: /home/exedev/repos/stock-situation-runtime
branch: codex/task-q2-pure-function
commit: 9472f4cf9fb1c91ed59575be9b0b66433ef738ca
subject: fix(replay): defer empty slices at auction barriers
remote: not configured
push/deploy: none
```

## Evidence and root cause

Real SELECT-only inspection of `market_data1.stock_tick_v2` for 2026-09-23
found zero rows in `[09:25:57,09:26:00)` and five rows, all at
`09:26:00.000`, in `[09:26:00,09:26:03)`. The empty first interval was assigned
its excluded right edge as `logical_ts_ms`. That made the previous frame emit
the rolling `latest` summary from pre-boundary state. The actual 09:26:00 rows
were read in the next half-open slice; the one-second latest throttle then
prevented a corrected close summary.

The pre-fix latest amount was `13,619,535,240`. The five affected symbols were
`002821`, `002935`, `300012`, `300062`, and `300136`; the difference from their
09:25 anchor amounts to their 09:26:00 values sums to `1,865,796`. The fixed
latest amount is `13,621,401,036`, matching both the Q2 `am` sum and the
deployed-release control.

The data supports this replay-order diagnosis for the observed date. TD
event-time evidence does not establish Rabbit arrival order or historical
`available_at`.

## Change and verification

The t1-v2 change:

- assigns an empty half-open frame `slice_end_ms - 1`, so it cannot claim the
  excluded right-edge second;
- registers 09:26:00 as the auction-close barrier, grouping rows at that second
  before the close summary;
- forces the final rolling auction summary at 09:26:00 even if the normal
  one-second throttle has not elapsed.

The regression self-test failed before the change and passed after it. Both
dev-minimal and full-dependency build/self-test passed. `git diff --check`
passed before commit. Full details and artifact hashes:

`/home/exedev/validation/t1v2-latest-boundary-repro-20260925/auction_close_boundary_report.md`

Real bounded replay used the same input in both runs:

```text
trade date: 2026-09-23
window: [09:15:00,09:26:03)
TD: one SELECT per 3-second half-open slice, read only
source_in=ticks=212027
source_reject=0
batches=222
clocks=1
td_sql=0
ack=0
```

Each run wrote only to a fresh Redis DB15 prefix. Across the pre-fix and fixed
runs, all 5,222 Q2 hashes and the active set were identical. Frozen 0920/0924/
0925 projections and the 0925 anchor archive were identical. The fix affects
the rolling close summary, not those frozen snapshots.

## Side-effect audit

- Redis writes: unique validation prefixes in DB15 only, 5,233 keys per run;
  DB0 matching-prefix count remained zero.
- TD writes: none (`td_sql=0`). Rabbit consume/ACK: none (`ack=0`).
- `engine-next` and `t1-v2-live` remained active; observed restart counts were
  zero. No service restart or deployment occurred.
- No production Redis namespace was used; no TD retention or source data was
  changed.

## Alignment and remaining work

This closes only the reproduced 09:26:00 empty-slice/latest-summary defect.
It does not establish exact Rabbit batch membership/order, historical
availability, universal auction semantics, full-session producer equivalence,
or NORMAL acceptance. `TASK-008` remains `PARTIAL_EVIDENCE`; Phase P remains
`PARTIAL`; `M3_1_NORMAL=BLOCKED`; `TD_WRITE_HEALTH=UNPROVEN`.

Next action remains within the current phase: independently audit this patch
against the stated replay contract and real-data output before considering any
next phase. Do not start a new migration stage from this evidence alone.
