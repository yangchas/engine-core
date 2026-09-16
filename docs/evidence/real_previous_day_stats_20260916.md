# Real PreviousDayStats verification — 2026-09-16

## Scope

Two bounded, read-only Core data-function probes were run against the
existing TD daily-kline access path. The function derives the requested
previous trade date from the calendar authority; the caller does not inject a
second expected date.

## Results

| Request trade date | Derived date | TD rows | Result | Reason |
|---|---|---:|---|---|
| `2026-09-16` | `2026-09-15` | 0 | `MISSING` | No daily rows returned for the derived date |
| `2026-09-09` | `2026-09-08` | 4 | `UNAVAILABLE` | Rows exist, but historical `available_at` is unknown |

The second case returned real values for all four requested symbols, including
close and amount, while retaining `available_at_ms=null` and
`missing_fields=("available_at_unknown",)`. The result therefore cannot be
used as a replay/runtime input for an earlier knowledge cutoff. This is the
intended fail-closed behavior; the query observation time is not used to
invent a historical publication time.

## Contract checks

```text
calendar-derived previous date       PASS
provider received exact derived date PASS
wrong-date fallback                  NOT USED
missing rows != unknown availability PASS
observed_at != available_at           PASS
TD writes                             0
Redis/Rabbit/effect writes            0
```

The 09-15 empty result is a current data-availability gap, not evidence that
the date derivation is wrong. It remains `MISSING` until the upstream TD
dataset contains that date.

## Runtime

```text
server: cobra-ion
TD access: existing taos read path, per-symbol d_<code> tables
Core probe commit: 336d8b9999cea1e0449b81deb3e0d788afd6b23d
```

