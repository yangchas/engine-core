# Minute window wheel parity

## Scope

`MinuteWindowTracker` extracts only the compact minute history and neutral
metrics used by the legacy `engine_next.runtime.tick_window_tracker`.
It is not connected to Engine and does not migrate any strategy threshold.

## Preserved behavior

- One canonical point per symbol and local minute.
- Price change uses the immediately preceding minute.
- Rolling amount uses the oldest available cumulative amount in the preceding
  two minute slots, matching the legacy native implementation's bounded
  lookback.
- A cumulative counter reset never becomes a positive delta.
- The new wheel keeps exactly the configured number of minute buckets.  The
  legacy Python helper applied a hidden minimum and retained an extra boundary
  bucket; that retention detail is intentionally not part of the new public
  contract.  The default horizon remains large enough for the two-minute
  metric.

## Deliberate contract corrections

- Source epoch milliseconds are required; there is no `time.time()` fallback.
- Symbols are validated by the core Q2 contract and qualified symbols are not
  silently stripped.
- Prices and amounts are explicit integer milli-price/yuan units.
- Missing references remain explicit (`None` plus a reason) instead of being
  replaced with zero.
- Same timestamp conflicting observations fail closed; identical repeats are
  idempotent.

## Verification

The wheel has Golden, Boundary and Adversarial tests in
`tests/test_time_windows.py`.  The legacy implementation is evidence for the
bounded amount lookback, not an oracle for the corrected input validation.
