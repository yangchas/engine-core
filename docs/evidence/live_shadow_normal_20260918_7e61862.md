# Live morning shadow NORMAL evidence — 2026-09-18

## Scope

The read-only Core shadow started before the auction window on `cobra-ion` and
consumed the real 09:26 and 09:32 wall-clock nodes.  `engine-next` and
`t1-v2-live` remained the production owners.  The shadow did not create a
Rabbit consumer, ACK or publish messages, write Redis/TD, emit a notification,
or restart a production service.

```text
runtime copy: /home/exedev/validation/engine-core-7e61862-v1
output: /home/exedev/validation/live-morning-shadow-20260918-7e61862
build identity: 7e61862
trade date: 2026-09-18
symbols: 000001, 000002, 600519
origin: NORMAL
```

## Startup

The process observed startup at `09:13:22.830+08:00`.  The premarket Q2
cohort had full row coverage but contained the midnight bootstrap state, so it
correctly remained `STALE / BEST_EFFORT_STALE` rather than being promoted to
READY.

Startup readiness was `PARTIAL`:

```text
previous_day_stats:       UNAVAILABLE
previous_day_limit_pool:  UNAVAILABLE
hot_plates:               MISSING
```

The auction reference preparation operation completed and froze its result
with status `PREPARED`; that status means the preparation operation ran, not
that all startup reference DataResults were READY.

## AUCTION_0926

```text
scheduled: 09:26:00.000
fired:     09:26:00.091
late:      91 ms
origin:    NORMAL
Q2:        PARTIAL / BEST_EFFORT_MIXED_FRESHNESS
coverage:  1.0
newest source record: 09:25:04
```

All three selected symbols had real TD 0920/0924/0925 rows.  The 0924→0925
`AnchorDeltaFactV1` result was READY for every symbol.  Each symbol executed
the in-memory Engine with nine signals and three fact-only strategy results;
the direct wheel and Engine fact semantic hashes were equal.  Source record
times were retained and were not rewritten to the business anchors.

The Engine fact status remained PARTIAL because the surrounding input quality
was partial.  A resolved individual fact does not upgrade the whole cohort.

## OPENING_0932

```text
scheduled: 09:32:00.000
fired:     09:32:00.010
late:      10 ms
origin:    NORMAL
Q2:        PARTIAL / BEST_EFFORT_MIXED_FRESHNESS
coverage:  1.0 (5224/5224)
stale symbols: 16
newest source record: 09:31:23
```

All three selected opening transition facts were READY.  The Core opening
strategy remained `FACT_ONLY / OBSERVE` and reported PARTIAL because the Q2
projection was partial.  The unproven generic `speed_1m` field remained null.

The legacy loader was deliberately `NOT_CONFIGURED` in this run, so this
artifact proves normal-origin Core execution but does not by itself prove a
same-input `engine-next` consumer differential.

## Production runtime observation

At 10:06 both production services were active with zero systemd restarts.
There were no `No enough disk space` messages on 2026-09-18 and no nonzero
`ack_fail` lines in the inspected window.  Root filesystem usage was 90%.

However, `t1-v2-live` wall lag had grown to approximately 500 seconds by
10:06.  This is an operational WARN and prevents treating later current-Q2
reads as fresh merely because the service is active.

## Safety and hashes

```text
new_rabbit_consumer:    0
rabbit_ack_or_publish:  0
redis_write:            0
td_write:               0
notification_or_effect: 0
production_restart:     0
```

| file | SHA-256 |
| --- | --- |
| `startup.json` | `0c27d30f22b4a97cb27df0e2d32606b273a9f4a6869e43e4b6384b11f9e32600` |
| `AUCTION_0926.json` | `974c468d748b6b17ded61a73366dd2b958fc1b17ae108aca1a80c150b93a3dd1` |
| `OPENING_0932.json` | `298f05fb6bf5aa2543542177c48bfa96f80a5b8cdf3d014d6e0d8dcf0ea63666` |
| `manifest.json` | `0f11a06060b16bc92f141585ce24b5286267ff4f2846bc879c48b4fa1bf54dfb` |

Local copies are under the ignored directory:

```text
engine_core/tmp/live-morning-shadow-20260918-7e61862/
```

## Acceptance impact

```text
NORMAL_TIMER_EXECUTION:              PASS
AUCTION_SELECTED_FACTS:              PASS
OPENING_SELECTED_FACTS:              PASS
ENGINE_WHEEL_SEMANTIC_PARITY:        PASS
STARTUP_REFERENCE_READINESS:         NOT CLOSED
FULL_ENGINE_NEXT_DIFFERENTIAL:        NOT RUN IN THIS ARTIFACT
CURRENT_RUNTIME_FRESHNESS:            WARN
ENGINE_CORE_REPLACEMENT:              NOT READY
```

This closes the missing normal-origin timer evidence for the bounded shadow.
The next migration work is startup reference readiness plus a same-input
legacy/Core business consumer differential; it is not another scheduler or
Replay abstraction.
