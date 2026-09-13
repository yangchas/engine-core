# Core test-scope audit (2026-09-14)

## Result

The default suite currently has **315 tests**. It is a deterministic offline
contract/fixture suite. All tests pass locally and in a Cobra-ion Python 3.12
isolated archive.

The files named `test_real_*` are not live network tests. Static inspection
shows they use fake Redis clients, fixture rows, connector doubles, or explicit
fetch overrides. They verify runner contracts and failure handling; they do
not prove that Redis, TDengine, BaoStock, Kaipanla, THS, Wencai, or RabbitMQ
are reachable at test time.

## Scope split

| Scope | Collected tests | Meaning |
|---|---:|---|
| Core/offline contract, wheel, engine and replay tests | 272 | repeatable behavior against fixtures and in-memory doubles |
| `test_real_*` contract/runner tests | 43 | deterministic tests of real-data command boundaries; fake/override inputs |
| Total | 315 | no implicit external I/O |

The `test_real_*` count includes real-named tests such as cache inventory,
reference probes, Redis/TD comparison, opening facts, and auction shadow. The
actual live evidence is produced separately by read-only Cobra commands and
must remain classified as external probe evidence, not as pytest coverage.

## Live-evidence boundary

The current production capture and probes are intentionally separate:

```text
pytest (offline, deterministic)
    !=
Cobra read-only Redis/TD/legacy connector probe
    !=
production service acceptance
```

This prevents a green test count from being misreported as real-data or
`engine_next` replacement acceptance. The active 2026-09-14 capture remains
the authoritative source for the next real Q2/auction evidence.
