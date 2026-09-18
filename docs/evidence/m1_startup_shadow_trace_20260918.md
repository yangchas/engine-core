# M1 Startup Shadow Trace Integration

## Scope

Commit `b4c2bc4` wires the pure `StartupCheckpointTraceV1` wheel into the
existing `examples/run_live_morning_shadow.py` startup evidence path. The
runner now records due 08:30/09:00 checkpoint traces in `startup.json`, while
the existing 09:26/09:32 shadow timer and node paths remain unchanged.

This is not a production startup replacement. It performs no provider
acquisition beyond the runner's existing read-only probes, adds no scheduler,
does not consume RabbitMQ, and does not write Redis/TDengine or trigger any
notification/effect.

## Verification

Local Windows:

```text
python -m pytest -q -p no:cacheprovider   511 passed
python -m compileall -q src tests examples  PASS
git diff --check                           PASS
```

Cobra-ion isolated validation copy
`/home/exedev/validation/engine-core-6511981-v1` using Python 3.12.3:

```text
python -m pytest -q -p no:cacheprovider   511 passed
python -m compileall -q src tests examples  PASS
```

Changed-file SHA-256 equality:

```text
examples/run_live_morning_shadow.py  89b32194b015d62c344851bc6649b6fd7f928626b8c293a166530a607aabb225
tests/test_live_morning_shadow.py    136af811172e6d6bfad606ccb27b47c9e2e22860d44911dbf689494b310f2c9e
```

## Safety boundary

`engine-next` and `t1-v2-live` remain the production owners. No production
service was restarted and no Rabbit ACK/publish, Redis write, TDengine write,
notification, order, or effect was used by this change.

## Remaining work

The next step is a bounded real-data startup shadow run with the existing
read-only Redis/TD providers. It must be run in an isolated output directory
and must retain `PARTIAL`/`UNKNOWN` readiness rather than turning absent 0924
or unavailable historical reference data into a normal result.
