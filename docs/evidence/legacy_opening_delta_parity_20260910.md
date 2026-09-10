# Legacy opening fact parity — 2026-09-10

## Scope

This evidence closes the first Gate B fact slice only.  It covers the pure
single-stock opening helpers extracted from the deployed `engine_next`
release; it does not claim parity for strategy thresholds, report rendering,
email delivery, or the full 09:25-to-open pipeline.

| Legacy capability | Legacy location | New wheel | Status |
| --- | --- | --- | --- |
| `after - before`, finite numeric operands | `engine_next/runtime/open_confirmation.py::_delta` | `compute_delta` | MATCH |
| positive-price opening percentage points | `::_open_fact` | `compute_open_change_pct` / `build_open_fact` | MATCH |
| independent limit-state validation | `::_open_fact` | `build_open_fact` | MATCH |
| delta labels | `::_state` | `classify_delta` | MATCH |
| sign reversal only when `before * after < 0` | `::_sign_state` | `classify_sign_state` | MATCH |
| percentage-point delta to basis points (`round(value * 100)`) | `::_change_bp` | `compute_change_delta_bp` | MATCH |

## Evidence source

- Legacy release: `/home/exedev/services/engine-next/releases/20260903_e272842`
- Core commit under test: `4cc7ccdd6e38a84261b41b89b5c21f9f21523947`
- Verification host: `cobra-ion` (Python 3.12 shared runtime)
- Remote isolated core checkout: `/tmp/engine-core-trash/engine-core-4cc7`
- Remote core archive SHA256: `002a258d81755cd6d9672191b3ee9dad34e575b26264989f52b928512c5308a7`

The legacy module was invoked directly on cobra-ion for the same vectors as
the new pure functions.  The new test suite keeps the resulting vectors as a
local oracle in `tests/test_opening_legacy_parity.py`; it does not import the
production release at test time.

## Verified vectors

The comparison covered:

1. valid price/previous-close and independent valid limit state;
2. valid price with malformed limit state;
3. zero price and unavailable limit state;
4. malformed price with string limit state;
5. positive, negative, zero, zero-touch, sign-reversal, and missing deltas;
6. basis-point conversion for normal, fractional, and unavailable values.

Remote direct differential result: all fields and labels matched.  Local
pytest and remote pytest both pass with the same fixture vectors.

## Real Redis sample

On the same server runtime, the legacy helper and the new wheel were also
called on the live Redis Q2 projection for `2026-09-10` and symbols
`000001`, `300750`, and `600519`.  All three rows returned `MATCH`, including
the source timestamp, opening percentage-point change, 2-minute amount,
independent limit-state status, speed value, and overall availability status.
The read path used `SMEMBERS/HGETALL` only; no production state was changed.

## Boundary statement

`OpeningFactV1` remains a fact-only contract.  No legacy threshold,
`TURN_STRONG`/`TURN_WEAK` conclusion, report projection, notification, or
external side effect is migrated by this evidence.
