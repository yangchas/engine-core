# Core Guardian

`examples/core_guardian.py` is a long-running, wall-clock scheduler. It has no cron integration and does not install a service. The default mode is dry-run. A configured calendar is required: `CORE_DATA_ROOT/trade_dates.json` (a JSON list of ISO trade dates, or an object with `trading_dates`). Missing or invalid calendar data fails closed; weekdays are never used as a substitute.

## Start

From the repository root:

```sh
PYTHONPATH=src:. python examples/core_guardian.py --dry-run
PYTHONPATH=src:. nohup python examples/core_guardian.py --run > ~/core-data/guardian.log 2>&1 &
```

`--run` invokes configured existing entry points. Redis publication requires `--write`; TD writes require both `--write --td-write`. Email requires `--email-self` and targets only `412826313@qq.com`. Readiness gates query TD/Core manifests read-only by default. `--gate-status JSON` overrides named gates for drills or emergency operation, and `--adapter module:function` may provide additional overrides. The close raw-capture schedule entry is skipped with a single `DISABLED_NOT_IMPLEMENTED` log line until a Core capture job exists.

Guardian readiness uses named engineering thresholds: 90% snapshot coverage, 85% auction report coverage, 4,000 checkpoint symbols, and a 4,500-symbol fallback when a trusted daily universe is unavailable. These values schedule startup work; they are not market laws and do not decide whether a report is valid.

Useful inspection modes:

```sh
PYTHONPATH=src:. python examples/core_guardian.py --list
PYTHONPATH=src:. python examples/core_guardian.py --once-now
PYTHONPATH=src:. python examples/core_guardian.py --simulate-date 2026-10-16 --simulate-clock 08:30:00..20:30:00 --calendar /path/to/trade_dates.json
```

`--simulate-clock` advances the fake wall clock by `--tick-seconds` (default one second), prints scheduled outcomes, and never runs handlers. The simulated date still must occur in the supplied calendar. `--events id1,id2` limits the event set.

## Event table

Windows are inclusive start/deadline points. Late starts within the window catch up. Past the deadline the event becomes `MISSED`; auction report may run degraded at its deadline if its coverage gate is still false. Every event is once per trade date. Retry attempts use exponential backoff, are capped by the event policy, and open a circuit after the configured maximum.

| Event family | Window / cadence | Gate and handler |
| --- | --- | --- |
| Startup, Kaipan refresh, premarket slice | 08:30, 08:50, 09:00 | Calendar; Core premarket data / Kaipan fetch |
| Auction monitoring and snapshot checks | 09:15–09:20; 09:20:03, 09:24:10, 09:25:10 | Snapshot/readiness gates; monitor/check handler |
| Auction report | 09:26:30–09:29 | 0925 coverage gate; degraded report at deadline |
| Opening reports | 09:32:10, 09:38:10, 09:45:30 | Checkpoint gate; staged open report |
| Kaipan hot capture | 09:16, 09:21, 09:26:30, 09:28:30, 09:31, 09:33, 09:40, 10:10, 10:20, 10:30, 10:45, 11:00, 11:25, 13:30, 14:00, 14:30, 14:50, 15:05 | Calendar; explicit external command adapter |
| Intraday data phase | Every 3 minutes, near `:05`, 09:33–14:57 | Checkpoint-ready gate; Core intraday phase |
| Close | 15:05 | Calendar; EOD capture adapter |
| Daily bars | Probe at 15:30; retry checkpoints 17:40, 18:30, 20:00 | Calendar triggers probe; postmarket publication waits for `daily_kline_ready` |
| Postmarket and night recap | 17:40–20:30; 01:00–08:29 | Daily bars ready / calendar; report entry point |

## State and health

`$CORE_DATA_ROOT/guardian/<trade-date>/state.json` stores each event's status, attempts, retry time, start/end time, duration, result/error and an embedded heartbeat. `$CORE_DATA_ROOT/guardian/heartbeat.json` is atomically replaced when state changes. Status values include `PENDING`, `WAITING`, `RETRY_WAIT`, `RUNNING`, `SUCCEEDED`, `SUCCEEDED_DEGRADED`, `MISSED`, and `CIRCUIT_OPEN`. A failure at the retry limit marks the process health `DEGRADED`. State writes use temp-file + `os.replace`; the process holds a non-blocking `flock` on `guardian/guardian.lock`.

## Legacy timeline mapping

| Legacy event | Core event | State / scope |
| --- | --- | --- |
| 08:30 and 09:00 startup checkpoints | `startup_0830`, `startup_0900` | Calendar-gated, once per date |
| 09:20:03 / 09:24:10 / 09:25:10 auction summaries | `auction_0920_check`, `auction_0924_check`, `auction_0925_check` | Readiness gates; Core checks only |
| 09:25+ missing-anchor repair | `auction_0925_check` recovery window | Bounded to 09:29:59; no legacy Redis repair |
| 09:26 pool refresh and auction analysis | `kaipan_refresh_0850`, `auction_report_0926` | Core Kaipan fetch and report; degraded report at 09:29 |
| 09:30–15:00 three-minute analyses | `intraday_HHMM` events and `open_report_*` | Checkpoint gate; event identities are trade-date scoped |
| Kaipan periodic hot-plate refresh | `kaipan_hot_HHMMSS` | Exact configured sampling times |
| 15:05 close mark | `close_mark_1505` and `kaipan_hot_150500` | Close capture adapter; no automatic Redis/TD write |
| 17:40 EOD lifecycle waits for Baostock | `baostock_probe_1530`, `baostock_retry_*`, `postmarket_publish` | Readiness is explicit; publication waits for daily bars |
| 17:40+ recap and night audit | `close_recap`, `night_recap` | Calendar/data gates; no automatic email |

## Extending

Add an `EventSpec` to `default_events()` with a unique ID, phase, start/deadline, named gate, handler, retry/timeout policy and optional dependencies. Provide gate/handler callables through `--adapter`; do not import the legacy runtime or write legacy keys. Keep handler inputs idempotent by trade date and stage.

## User-service draft (not installed)

```ini
[Unit]
Description=Core Guardian
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/repos/engine_core
Environment=CORE_DATA_ROOT=%h/core-data
ExecStart=/usr/bin/python3 %h/repos/engine_core/examples/core_guardian.py --run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Installing or enabling this unit requires separate user approval.
