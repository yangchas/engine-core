# StartupReadinessV1 final-commit verification — 2026-09-14

This is the final-commit verification companion to
`startup_readiness_probe_20260914.md`.

```text
verified commit: 4a9e548
archive SHA-256:  aa60f500307090edb53d38169085da2bea66d76d142cfe9a865cbff31b90f3b7
local tests:     386 passed
Cobra tests:     386 passed (Python 3.12.3)
compileall:      PASS (local and Cobra)
```

The final commit archive was expanded in a new directory on `cobra-ion` and
the exact same test and compile commands were run there. The real read-only
probe used the existing Redis Q2 adapter and wrote:

```text
remote artifact:
/home/exedev/validation/engine-core-4a9e548/startup-readiness-20260914-final.json
SHA-256:
9db3f8a581b1d545a5138899cacc3f49c5580b8f00fc9fcbf72ed2da6caa85b6

Q2 coverage: 1.0
Q2 status: STALE
Q2 consistency: BEST_EFFORT_STALE
readiness: PARTIAL
phase: POSTMARKET
due timers: AUCTION_0926, OPENING_0932
```

The Q2 source-time range and semantic content hash are preserved in the
artifact. The probe exposed only Redis `SMEMBERS`/`HGETALL`; it did not start a
consumer, acknowledge RabbitMQ, write Redis/TD, send notifications, or invoke
an effect. `engine-next` and `t1-v2-live` stayed active with zero restarts.

This verifies the side-effect-free readiness wheel and real Q2 read path. It
does not verify production startup ownership, source freeze ownership,
report/effect ownership, or cross-restart durable identity.
