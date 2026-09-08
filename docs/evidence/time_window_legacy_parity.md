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
- Within one minute, the latest source-time price/current amount remain the
  current observation while the greatest cumulative amount becomes the
  reference used by later minutes.  This follows the production `t1_v2`
  minute-ring behavior and prevents a transient lower counter from weakening
  the next minute's baseline.
- Production `t1_v2` overwrites the minute price in arrival order.  The new
  wheel intentionally selects the greatest source timestamp instead because
  recorded arrival order is unavailable in fixture/event-time replay.  It
  does not claim recorded-arrival parity for out-of-order prices.
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
- At the retained latest source timestamp, conflicting observations fail
  closed and identical repeats are idempotent.  The wheel does not retain an
  unbounded per-timestamp journal and therefore does not claim it can identify
  every conflict among older same-millisecond rows.
- An older source-time observation cannot overwrite a newer current
  observation in the same minute, but it may contribute a greater cumulative
  amount and its own reference timestamp.  This makes the minute reference
  independent of arrival order and intentionally differs from the legacy
  Python fallback.
- The minute maximum amount and its source timestamp are part of
  `MinuteWindowMetrics` and its semantic hash because this state affects later
  rolling results; it is not hidden mutable tracker state.

## Verification

The wheel has Golden, Boundary and Adversarial tests in
`tests/test_time_windows.py`.  The legacy implementation is evidence for the
bounded amount lookback, not an oracle for the corrected input validation.
