# guardiand

Build with Go 1.22 and the standard library:

```sh
./scripts/build.sh
```

Migrate the legacy schedule, then validate the generated config before running:

```sh
bin/guardiand migrate --from /home/exedev/repos/engine_core/src/engine_core/guardian/schedule.json --to /home/exedev/core-data/guardian/guardiand.json --expand
bin/guardiand validate /home/exedev/core-data/guardian/guardiand.json
```

Start one instance with a dedicated state root:

```sh
bin/guardiand run --config /home/exedev/core-data/guardian/guardiand.json --state-root /home/exedev/core-data/guardian/native-state
```

Window events use `trigger.start` as the earliest start time and
`trigger.deadline` as the latest start time, including time spent waiting for a
gate. `timeout` is the independent runtime budget (up to 24 hours). Optional
`late_until` (`HH:MM:SS`, same day) permits an event that has not succeeded to
start or resume after its deadline; if omitted, the event is missed at its
deadline as before.

`retry.max_attempts` is the total failed-attempt limit for one instance, including
all `RETRY_WAIT` rounds; zero or omission uses one attempt. An event with
`retry.until_late: true` must set `late_until` and runs one attempt per round.
Retryable failures wait `retry.backoff × 2^(attempts-1)` (default 60 seconds,
capped at 10 minutes and at `late_until`). The attempt count and next due time
are persisted, so a restart resumes the same retry sequence. Exhausting the
attempt limit ends with `FAILED_FINAL` / `RETRIES_EXHAUSTED`; reaching
`late_until` first ends with `RETRIES_EXHAUSTED_BY_LATE_UNTIL`.

For events without `until_late`, retries run inside the event and delay by
`retry.base × 2^(attempts-1)` (default 1 second, capped at 5 minutes). The
generated config sets `max_attempts: 3` and `backoff: "60s"` for premarket,
Kaipan fetch, postmarket, Baostock probe, calendar refresh, and night report
data events. Other events retain their configured attempt limit. Events marked
`silent_until_last` suppress the terminal retry alert; other exhausted events
emit one alert. Auction, open, and close report events retain their in-window
degrade handling.

The local Unix socket is `<state-root>/guardiand.sock` (mode `0600`). Send one UTF-8 JSON line and read one response line. For example, Python can send `{"t":"req","id":"1","op":"status","args":{}}`; `hello` uses `{"t":"hello","proto":1,"client":"local"}`. Supported operations include `status`, `list`, `trigger`, `pause`, `resume`, `skip`, `reload`, and `config.validate`. In `fakeclock` mode, `clock.advance` advances the injected clock without sleeping.

`phases` is an ordered, contiguous full-day list of `{name,start,end}` using
`HH:MM` boundaries; when omitted, guardiand uses the twelve A-share phases
from NIGHT through POST_MARKET. On dates absent from the configured trading
calendar, the current phase is `NON_TRADING`. Events may set `phases` to a
whitelist (or `any`) and `on_start: "reconcile"` to request a late catch-up
after boot. `on_phase_mismatch` defaults to `skip`, preserving the legacy
`SKIPPED_PHASE` terminal state. Set it to `defer` to record `DEFERRED`, its
current phase reason, and `next_phase`; scheduling starts the instance when an
allowed phase arrives. A deferred instance past its `late_until` (or deadline
when absent) becomes `MISSED` and is included in the missed summary.

Late data with a registry `fetch_job` has one startup catch-up path:
`boot_apply_plan` owns the fetch, so its matching window event does not also
set `on_start: "reconcile"`. The window event remains scheduled and retains
its late retry window for failures while the process is running. Report events
without a registry fetch job may keep `on_start` for late reporting.

Events may set `cost` to `light`, `medium` (the default), or `heavy`. Light
work bypasses class concurrency limits; medium work uses the configured class
slot; heavy work uses one independent global slot and reports waiters as
`QUEUED_HEAVY`. All work shares a 16-process hard cap. `heavy_max_load1`
defaults to `0` (disabled), accepts a numeric load-1 threshold or `"cpus"`,
and delays heavy starts by 60 seconds while `/proc/loadavg` is above the
threshold. Load deferrals are recorded as `DEFER_LOAD` without consuming
attempts or emitting alerts.

The heavy slot is held only during a handler attempt. Gate polling, load
deferral, and retry backoff do not reserve it; each attempt takes a FIFO place
and deadline expiry is evaluated while it waits. The process cap is also
released during a heavy retry delay. Light and medium admission are unchanged.

Startup events use `trigger: {"type":"startup","order":N,"after":[...]}`.
They run once per process boot in order after their listed startup dependencies;
failed dependencies produce `SKIPPED_DEP`. Handler requests include `phase`
and `now`, and `status` reports `phase`, `next_phase`, and the `boot` result
summary. Startup and reconciliation outcomes are included in the batched
startup `MISSED` summary.
