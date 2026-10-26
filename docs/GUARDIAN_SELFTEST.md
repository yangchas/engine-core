# Guardian self-test suite

`examples/guardian_selftest.py` builds a private `guardiand` binary from
`ROOT/native/guardiand`, then creates one config, state root, local calendar,
and `SELFTEST_DIR` per scenario. The scenarios run concurrently but never share
state. Their handlers only append JSONL records under the scenario directory;
they do not contact services or read `~/core-data`.

Run the short wall-clock suite with:

```sh
/home/exedev/services/engine-next/shared/venv/bin/python examples/guardian_selftest.py \
  --mode fast --work-dir /home/exedev/validation/s1/C544
```

`fast` schedules S01–S14 about 5–25 seconds ahead and compresses the production
window from 08:30:00–09:45:30 (4,530 seconds) onto 45 seconds for S15. Each S15
start is mapped linearly, with at least one second between starts to preserve
order at the scheduler's second-level resolution. Generated windows retain a
12-second run allowance so a scheduler tick does not close them immediately.
The run budget is 105 seconds; the requested maximum is 120 seconds.

Run the longer manual timing mode with:

```sh
/home/exedev/services/engine-next/shared/venv/bin/python examples/guardian_selftest.py \
  --mode minutes --minutes 3 --work-dir /home/exedev/validation/s1/C544
```

In this mode S01 starts at +2 minutes, S08 at +3 minutes, and S15 at +5
minutes; S15 maps the same 4,530-second source span onto six minutes. Thus the
full run takes about eleven minutes. `--minutes` is retained as an explicit
manual-mode parameter and currently requires a value of at least 3.

## Scenarios

| ID | Check |
|---|---|
| S01 | Window handler call is in `[start, start+2s]`; terminal state is `SUCCEEDED`. |
| S02 | Failed startup dependency is `SKIPPED_DEP`; `order`/`after` call ordering is checked; a failed normal-window `depends_on` must also skip. |
| S03 | `fail:2`, three attempts, 2s then 4s backoff, final `SUCCEEDED`. |
| S04 | `fail_always`, three attempts, terminal failure, exactly one local alert record. |
| S05 | `retry.until_late`, 3s base exponential backoff, and `RETRIES_EXHAUSTED_BY_LATE_UNTIL`. |
| S06 | Past start with `run_in_window`, past start with `skip`, and expired window (`MISSED`), each isolated in this config. |
| S07 | Gate remains blocked through deadline; `on_deadline=degrade` must set `degrade_requested` and succeed degraded; `missed` ends as `MISSED` without a handler call. |
| S08 | Gate becomes ready 2.5s after the event starts; handler must start within the 1s gate poll plus 1s tolerance. |
| S09 | `sleep:6` with `timeout=2s`; both attempts end as `HANDLER_TIMEOUT` and the handler is retried. |
| S10 | Response mapping for `ok`, `degraded`, `not_applicable`, and one-attempt permanent failure. |
| S11 | Calendar file omits today; `trading_day` must not run while `every_day` does. Both use `phases=["any"]` so phase filtering cannot hide calendar behavior. |
| S12 | Args round-trip unchanged; records `data_date` for offsets -1 and 0. Current behavior is that both equal `trade_date`; no calendar-day or trading-day offset is applied. |
| S13 | Three startup events ordered with `order`/`after` all run before the scheduled window event. |
| S14 | Kill the scenario daemon while one handler sleeps, restart with the same state root, and check the completed event stays at one call while the interrupted event reaches attempt 2. |
| S15 | Expands the 26 window events from `startup_0830` through `open_report_0945`; swaps handlers and configured gates for local self-test implementations, sets calendars to `every_day`, and checks one call per event in source-time order. |

The ±1.5s retry tolerance accounts for integer-second configured backoffs,
handler process startup, and the scheduler tick. S01 uses a two-second upper
bound because the live scheduler polls rather than firing a real-time timer.
S08 uses `gate.poll + 1s` for the same polling and process-start costs. These
limits are kept narrow enough to detect a changed backoff or a missed trigger.

## Runtime expectations

- A failed startup dependency and its dependent window are both `SKIPPED_DEP`;
  a pending window dependency defers, then releases on success or skips on
  terminal failure.
- `run_in_window` catches up after start while the window remains open;
  `skip` records a late start as `MISSED`.
- A gate still blocked at deadline invokes a `degrade` handler once with
  `degrade_requested=true`; `missed` records `MISSED` without invoking it.
- A date absent from a covered `trading_day` calendar is `EXCLUDED` and the
  event is `SKIPPED_NA`; an unavailable or out-of-range calendar remains
  `UNKNOWN` and is not excluded. Unreadable or malformed configured calendar
  files follow the existing error handling.
- `data_date_offset` is not applied in request construction. Both tested
  offsets send `data_date == trade_date`.
- Retry intervals use `eventRetryDelay`: configured base multiplied by
  `2**(attempt-1)`, capped at five minutes. The tested 2s and 3s bases produce
  2/4s and 3/6/12s intervals.

The first four bullets leave S02, S06, S07, and S11 expected to report FAIL
against the current source. This is visible evidence of missing runtime
behavior, not a passing result. S12 passes by recording the current offset
semantics.

## Handler replacement table

The self-test table is derived from the current
`src/engine_core/guardian/schedule.json` window events. Real-handler
acceptance remains `未验收` for every row. After each real handler passes its
own data-function acceptance, change only that event's handler from the
self-test handler back to the real handler and rerun the suite.

| Event ID | Current test handler | Real handler | Real-function acceptance |
|---|---|---|---|
| `startup_0830` | `guardian_selftest_handler.py run` | `premarket_data` | 未验收 |
| `kaipan_refresh_0850` | `guardian_selftest_handler.py run` | `kaipan_fetch` | 未验收 |
| `premarket_slice_0850` | `guardian_selftest_handler.py run` | `premarket_data` | 未验收 |
| `startup_0900` | `guardian_selftest_handler.py run` | `premarket_data` | 未验收 |
| `auction_watch_0915` | `guardian_selftest_handler.py run` | `auction_watch` | 未验收 |
| `kaipan_hot_091600` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `auction_0920_check` | `guardian_selftest_handler.py run` | `snapshot_check` | 未验收 |
| `kaipan_hot_092100` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `auction_0924_check` | `guardian_selftest_handler.py run` | `snapshot_check` | 未验收 |
| `auction_0925_check` | `guardian_selftest_handler.py run` | `snapshot_check` | 未验收 |
| `auction_facts_0925` | `guardian_selftest_handler.py run` | `auction_report` | 未验收 |
| `auction_report_0926` | `guardian_selftest_handler.py run` | `auction_report` | 未验收 |
| `kaipan_hot_092630` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `kaipan_hot_092830` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `kaipan_hot_093100` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `open_report_0932` | `guardian_selftest_handler.py run` | `open_report` | 未验收 |
| `kaipan_hot_093300` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `intraday_33` | `guardian_selftest_handler.py run` | `intraday_data` | 未验收 |
| `open_report_0935` | `guardian_selftest_handler.py run` | `open_report` | 未验收 |
| `intraday_36` | `guardian_selftest_handler.py run` | `intraday_data` | 未验收 |
| `intraday_39` | `guardian_selftest_handler.py run` | `intraday_data` | 未验收 |
| `open_report_0940` | `guardian_selftest_handler.py run` | `open_report` | 未验收 |
| `kaipan_hot_094000` | `guardian_selftest_handler.py run` | `kaipan_hot_capture` | 未验收 |
| `intraday_42` | `guardian_selftest_handler.py run` | `intraday_data` | 未验收 |
| `intraday_45` | `guardian_selftest_handler.py run` | `intraday_data` | 未验收 |
| `open_report_0945` | `guardian_selftest_handler.py run` | `open_report` | 未验收 |

## Known gaps

`examples/guardian_selftest.py::KNOWN_GAPS` is empty after C545. The fast
self-test expects all 15 scenarios to pass; its `start`, `backoff`, and
`catchup` fault modes must each make the corresponding scenario fail.
