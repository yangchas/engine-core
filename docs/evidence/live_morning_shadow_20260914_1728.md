# Live Morning Shadow V1 — 2026-09-14 17:28

## Code and environment

- Core commit: `472bf7d`
- Archive: `engine-core-472bf7d.tar`
- Archive SHA-256: `0cb91fd65fab3cdc251558c74f284e803a265c7691f0c834ae42487915faecf7`
- Cobra runtime: `/home/exedev/services/engine-next/shared/venv/bin/python` — Python 3.12.3
- Local and Cobra exact suite: `391 passed`; `compileall -q src tests examples` PASS

## Read-only run

- Remote output: `/home/exedev/validation/live-morning-shadow-20260914-1728`
- Symbols: `000001,000002,600519`
- Trade date: `2026-09-14`
- Start/stop shell window: `00:00:00–23:59:00` (late-start validation only)
- Actual observation: `2026-09-14T17:28` local time; both nodes were correctly marked `RECOVERY_CATCHUP`.
- Node files: `AUCTION_0926.json`, `OPENING_0932.json`; both business anchors remain 09:26/09:32 and source observation time is retained rather than rewritten.
- Startup Q2 was read through the existing Redis adapter. Opening node retained Q2 `coverage=1.0`, `quote_count=5220`, `status=STALE`, `consistency_status=BEST_EFFORT_STALE`, with source-time range preserved.
- The existing engine-next auction loader was invoked through `GuardRedis`; TD access was bounded `SELECT` through the existing `auction_snapshot_v2` query shape.

## Artifact hashes

```text
AUCTION_0926.json  e2b25f291e92e32f4ed7d788a32853512e24f8ea80bca5b44dd249ad92d6ff53
OPENING_0932.json  d54424446aabcb5b63a6bc8336357601001c20cd95ccaac766e54774a4439abc
manifest.json      46f3ba590bd9f906b6b875330992b8955f5e74370f9469c8bfaf277d3fbf1496
startup.json       c2a76f5f0779b172464cc9dd5abb5f923a156e334078e0c837c35f8ec878b6c3
```

Manifest safety counters were all zero:

```text
new_rabbit_consumer=0
rabbit_ack_or_publish=0
redis_write=0
td_write=0
notification_or_effect=0
production_restart=0
```

## Interpretation

This proves the bounded Core read-only composition can run against the real
Redis/TD/legacy-loader paths after a late start. It is not a 09:26/09:32
in-session timing proof: the observed inputs were obtained at 17:28 and are
therefore not presented as the values visible at the morning deadlines. The
next trading-day run must start before 09:15 and capture each node at its real
due time. `engine-next` and `t1-v2-live` remain production owners.
