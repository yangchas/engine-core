# Core test-scope audit (2026-09-14)

## Result

The original audit snapshot at this document's first commit had **322 tests**.
The current HEAD (`8d921b4`) collects **397 tests** locally; the additional
cases are still deterministic offline contract/fixture tests.  The
code-equivalent Cobra-ion archive (`81e0dd3`; later commits are documentation
only) also passed 397 tests under Python 3.12.3.  These counts remain an
offline suite result, not live-source acceptance.

The files named `test_real_*` are not live network tests. Static inspection
shows they use fake Redis clients, fixture rows, connector doubles, or explicit
fetch overrides. They verify runner contracts and failure handling; they do
not prove that Redis, TDengine, BaoStock, Kaipanla, THS, Wencai, or RabbitMQ
are reachable at test time.

## Scope split

| Scope | Collected tests | Meaning |
|---|---:|---|
| Core/offline contract, wheel, engine and replay tests | historical 279 | repeatable behavior against fixtures and in-memory doubles |
| `test_real_*` contract/runner tests | historical 43 | deterministic tests of real-data command boundaries; fake/override inputs |
| Current total | 397 | no implicit external I/O |

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

The current Cobra exact-archive verification also checks archive layout.  The
repository-root paths (including `examples/`) must be preserved; an archive
extracted with an extra `--strip-components=1` is invalid evidence because it
causes import/collection errors unrelated to Core behavior.  The corrected
code-equivalent archive passed all 397 tests and `compileall`; see
`docs/evidence/cobra_exact_verification_20260914_current.md`.
