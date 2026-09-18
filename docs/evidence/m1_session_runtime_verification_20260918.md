# M1 Session Runtime Coordinator Verification — 2026-09-18

## Scope

This evidence covers the minimal in-memory `SessionRuntimeCoordinator` added by
source commit `a452933`, plus the follow-up `RuntimePoll` trade-date/readiness
identity guard in this verification commit. It composes the existing calendar, session plan,
readiness and timer wheels. It does not start a service, consume RabbitMQ,
write Redis/TD, persist checkpoints, or emit effects.

## Offline contract verification

| Check | Result |
|---|---|
| Local Python test suite | `461 passed in 1.67s` |
| Local `compileall -q src tests` | PASS |
| Local `git diff --check` | PASS |
| Remote Python environment | `/home/exedev/services/engine-next/shared/venv/bin/python` |
| Remote validation directory | `/home/exedev/validation/engine-core-6511981-v1` |
| Remote Python test suite | `461 passed (after syncing the final runtime/test files)` |
| Remote `compileall -q src tests` | PASS |

The remote validation directory received only the three changed runtime files
(`session_runtime.py`, `__init__.py`, and `test_session_runtime.py`) from the
same local source commit. It is not the production checkout.

## Real read-only Q2 smoke on cobra-ion

At `2026-09-18 12:50 CST`, the existing read-only probes were run against the
production Redis Q2 path from the isolated validation copy:

- Q2 rows: `5224/5224`, row coverage `1.0`.
- Newest source lag: `6711` seconds; all `5224` symbols were stale under the
  explicit 10-second freshness policy.
- Q2 status: `STALE`; readiness status: `PARTIAL`.
- Repeated observation engine/probe hashes were equal.
- Read operations were bounded to Redis `SMEMBERS/HGETALL`; no Redis/TD
  writes, Rabbit consumption/ACK, notification, or effect path was assembled.
- Startup readiness remained fail-closed/partial and exposed due timer ids
  `AUCTION_0926` and `OPENING_0932` for late observation; it did not promote
  stale data to `READY`.

Artifacts produced remotely:

```text
/tmp/engine_core_live_q2_20260918_1249.json
sha256: efea2f4fd521a334783637b834478dd4455662241c513ecf68f074831a2b4a48

/tmp/engine_core_startup_20260918_1249.json
sha256: 6efe6ef5d3d09ab5834f18378e571686916f234f8adba6c68ceee6d18c96ac21
```

## Acceptance interpretation

The coordinator and its local/remote contracts are verified. The live Q2
observation is real but stale, so this is not normal-time opening acceptance
and does not authorize replacing `engine-next`. Production `engine-next` and
`t1-v2-live` remained active; the next safe step is to integrate this
coordinator into the existing read-only morning shadow and wait for a fresh
Q2/reference-data observation before considering any production migration.
