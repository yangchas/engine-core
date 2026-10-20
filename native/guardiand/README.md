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

For data events, `retry.until_late: true` keeps retryable handler failures in
`RETRY_WAIT` through `late_until`; `retry.backoff` is the exponential base
(capped at 10 minutes). Each retry wait is written to the ledger with its next
attempt time, and emits no alert while waiting. At the cutoff, the event is
`FAILED_FINAL` with `RETRIES_EXHAUSTED_BY_LATE_UNTIL` and one alert. Permanent
failures and timeouts configured with `no_retry_on_timeout` remain immediate
terminal failures. The generated config marks the first three Baostock probes
(17:30, 17:40, 18:30) `silent_until_last`, so their cutoff is quiet; the 20:00
probe alerts at its own cutoff. Auction, open, and close report events do not
use `until_late`; their in-window degrade handling remains in effect.

The local Unix socket is `<state-root>/guardiand.sock` (mode `0600`). Send one UTF-8 JSON line and read one response line. For example, Python can send `{"t":"req","id":"1","op":"status","args":{}}`; `hello` uses `{"t":"hello","proto":1,"client":"local"}`. Supported operations include `status`, `list`, `trigger`, `pause`, `resume`, `skip`, `reload`, and `config.validate`. In `fakeclock` mode, `clock.advance` advances the injected clock without sleeping.

`phases` is an ordered, contiguous full-day list of `{name,start,end}` using
`HH:MM` boundaries; when omitted, guardiand uses the twelve A-share phases
from NIGHT through POST_MARKET. On dates absent from the configured trading
calendar, the current phase is `NON_TRADING`. Events may set `phases` to a
whitelist (or `any`) and `on_start: "reconcile"` to request a late catch-up
after boot. Window events outside their phase are recorded as `SKIPPED_PHASE`.

Startup events use `trigger: {"type":"startup","order":N,"after":[...]}`.
They run once per process boot in order after their listed startup dependencies;
failed dependencies produce `SKIPPED_DEP`. Handler requests include `phase`
and `now`, and `status` reports `phase`, `next_phase`, and the `boot` result
summary. Startup and reconciliation outcomes are included in the batched
startup `MISSED` summary.
