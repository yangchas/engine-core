# M1 Startup Checkpoint Trace Evidence

## Scope

Commit `b7c1c25` adds the small, pure `StartupCheckpointTraceV1` wheel for
the legacy 08:30 and 09:00 startup checkpoints. It records the business
anchor separately from the actual readiness observation time and hashes the
Q2/readiness and timer-firing identities included in the trace.

This is an evaluation/trace boundary only. It does not acquire providers,
repair data, persist checkpoints, consume RabbitMQ, write Redis/TDengine, send
notifications, or trigger effects. It does not make `engine_core` the
production startup owner.

## Verification

Local Windows:

```text
python -m pytest -q -p no:cacheprovider   511 passed
python -m compileall -q src tests          PASS
git diff --check                           PASS
```

Cobra-ion isolated validation copy
`/home/exedev/validation/engine-core-6511981-v1` using the existing Python
3.12.3 runtime:

```text
python -m pytest -q -p no:cacheprovider   511 passed
python -m compileall -q src tests          PASS
```

Changed-file SHA-256 equality:

```text
src/engine_core/startup_readiness.py  6e1359075c55684cf740e51e84b1028eb9bdde90e1d8e2608b55e94e312e971b
src/engine_core/__init__.py           ceafd61c6a977e2a8ce939f38c1535f02a0279c71ccd323d989725f32878f5cd
tests/test_startup_readiness.py       9b1c057f9b2743a1438016bcf03a7a95964e18f02c6c4e9ee3c3a58ebd02d2aa
```

## Safety

`engine-next` and `t1-v2-live` were not restarted or modified. No production
RabbitMQ consumer/ACK, Redis write, TDengine write, notification, order, or
other effect was used by this verification.

## Remaining boundary

The deployed legacy process still owns startup acquisition/repair and the
0920/0924/0925 source freeze. This wheel only makes the already-observed
08:30/09:00 readiness decision traceable. The next migration step is to wire
it into a bounded read-only startup shadow, not to replace the production
startup path.
