# Real TD previous-day DataFunction validation — 2026-09-10 12:44 CST

## Scope

The isolated core checkout on `cobra-ion` executed the existing
`engine_next` TD query shape (`d_<symbol>` child table, inclusive date bounds)
through a new thin `TDPreviousDayStatsProvider`, then ran
`PreviousDayStatsFunction` and `TemporalDataGuard`. No legacy mutating
`TDengineService` constructor was instantiated. The command performed only
bounded `SELECT` calls for `600519`, `000001`, and `300750`.

## Evidence

| Item | Value |
|---|---|
| Host | `cobra-ion` |
| Core commit | `f2dbad24e5107e4ddf2aa93dd806d9a86f490899` |
| Verification archive SHA-256 | `e37b4f41e839f666efdd578d9607c9259a51c1c8dc3ef0912c1cc610f249c3f8` |
| Requested trade date | `2026-09-10` |
| Derived previous trade date | `2026-09-09` |
| Symbols | `600519`, `000001`, `300750` |
| TD rows read | `3` |
| Actual returned date | `2026-09-09` |
| Result status | `UNAVAILABLE` |
| `available_at_ms` | `null` |
| Result semantic hash | `f0e732f31c5cdc274c9f964f3e060c86375f01006ff0265db99adb15040cf4e0` |
| Result file SHA-256 | `c3a02aef35eb614106031cf0b5a5b5c023bf9709ccc3266a208944e0de12ee87` |
| Side effects | none; TD `SELECT` only |

## Interpretation

The physical TD read and date derivation passed: the provider received the
calendar-derived `2026-09-09` and returned three normalized rows with the
same actual date. The final `UNAVAILABLE` status is intentional and correct:
the live query provides no historical publication timestamp, so the guard
does not infer that the data was knowable before the evaluation cutoff.
This result is therefore suitable for live data-path verification and an
offline evidence capture, but it is not promoted to a replay runtime input.

