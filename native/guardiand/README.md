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

The local Unix socket is `<state-root>/guardiand.sock` (mode `0600`). Send one UTF-8 JSON line and read one response line. For example, Python can send `{"t":"req","id":"1","op":"status","args":{}}`; `hello` uses `{"t":"hello","proto":1,"client":"local"}`. Supported operations include `status`, `list`, `trigger`, `pause`, `resume`, `skip`, `reload`, and `config.validate`. In `fakeclock` mode, `clock.advance` advances the injected clock without sleeping.
