# engine-next / Core same-input Q2 differential — 2026-09-17

## Scope

This was a bounded, read-only audit on `cobra-ion` after the opening nodes had
already passed.  It did not recreate a historical 09:26 observation.  Its
purpose was narrower: capture the exact Redis quote hashes consumed by the
deployed `engine_next` context path for three symbols and apply the Core Q2
normalizer/opening fact wheel to those immutable raw hashes.

Production ownership did not change.  No Rabbit consumer or ACK path was
added, Redis/TD were not written, and no report, notification or order effect
was invoked.

## Runtime identity

```text
Core commit: 88cd71f0eecf97b4f2d1539ef952340996711285
Core archive SHA-256: 267f4015331613ca280ce72db42a662a5335c34b753d56c9b59d6c86e3aaeb21
Legacy release: /home/exedev/services/engine-next/releases/20260903_e272842
Remote evidence: /home/exedev/validation/engine-next-read-probe-20260917-88cd71f-v1
Symbols: 000001, 000002, 600519
```

Local and Cobra Python 3.12.3 both ran the exact archive with `452 passed` and
`compileall` PASS.  The archive hash matched across hosts.

## Exact legacy read boundary

`EngineNextContextProbeV3` records only the requested `stock:quote:*` and
`q2:*` HGETALL results.  It supports the existing Redis pipeline without
changing result order and rejects unclassified pipeline methods.  The probe
observed empty legacy `stock:quote:*` hashes and the following actual Q2 keys:

| symbol | source_record_time_ms |
| --- | ---: |
| 000001 | 1789613739000 |
| 000002 | 1789613739000 |
| 600519 | 1789613741000 |

`guard_writes=[]` and `read_only=true`.

## Same-input differential

The captured Q2 hashes, rather than a second Redis observation, were passed
through `normalize_q2()` and `build_open_fact()`.  Comparisons use the source
contracts explicitly:

| Contract | Result |
| --- | --- |
| Core percentage points vs legacy ratio x 100 | MATCH 3/3 (floating tolerance `1.2e-14`) |
| `amount_2m_yuan` | MATCH 3/3 |
| Q2 `speed_1m_bp / 10000` vs legacy `speed_1m` ratio | source transform observed 3/3 |
| Core opening fact `speed_1m` | deliberately `None` 3/3; units are not silently conflated |

The deployed context does not always treat Q2 `am` as the final auction
amount authority.  For `600519`, the captured Q2 value was `17,611,700`, while
the legacy context value was `16,856,932`; the latter matches the separately
observed 0925 auction projection path.  This is a source-priority difference,
not a calculation mismatch, and Core must not replace the frozen auction
reference with current Q2 `am`.

## Non-atomic second read

The subsequent live Core Redis read was intentionally kept as separate
evidence.  Q2 advanced for `000001` and `000002` between calls, so those rows
are ASOF/UNPROVEN for cross-call equality; `600519` retained the same source
timestamp.  The live cohort had `coverage=1.0` but remained `STALE`.  No value
match from different source timestamps is promoted to exact parity.

The Core validation runner was also corrected so Q2 `speed_1m_bp` remains in
source metadata and is not injected into the opening fact's unproven generic
`speed_1m` field.

## Evidence hashes

| file | SHA-256 |
| --- | --- |
| `context.json` | `895e871e568c9f281f1e40e5244dd783340a1bb769b08e65d3b8bb74c2dc79e6` |
| `core_opening.json` | `37e7ddb70032994c4b34abcb1d0782f6293fa3120dbb05d02dd837b84f088684` |

Local copies are under the ignored validation directory:

```text
engine_core/tmp/engine-next-read-probe-20260917-88cd71f-v1/
```

## Acceptance impact

```text
ENGINE_NEXT_CONSUMPTION_ACCEPTANCE:
  PASS for the bounded Q2 price/amount2m source transformations
  OBSERVED for speed conversion
  PARTIAL for auction source precedence

ENGINE_CORE_SHADOW_ACCEPTANCE:
  PASS for truthful same-input normalization and missing-unit behavior

JOINT_TRADING_DAY_ACCEPTANCE: WARN
```

This closes the previous non-atomic Q2 comparison gap for the bounded fields.
It does not prove runtime batch membership, full-market report parity, plate
label encoding, normal-origin timing or production replacement readiness.
