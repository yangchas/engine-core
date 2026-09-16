# Core ↔ engine-next opening differential — 2026-09-16

## Scope

This is a bounded, read-only differential of the Core opening fact wheels
against the deployed `engine_next` pure opening helpers. It uses the same real
Redis Q2 projection and reads the real TD auction rows when available. It does
not run a Core strategy, write Redis/TD, consume Rabbit, send notifications or
trigger effects.

## Verification

| Item | Value |
|---|---|
| Server | `cobra-ion` |
| Core commit | `71f483a8c667e776cc7309d13d8c11cb4febe95e` |
| Legacy release | `/home/exedev/services/engine-next/releases/20260903_e272842` |
| Trade date | `2026-09-16` |
| Symbols | `000993, 300207, 600330, 600519` |
| Redis projection | `coverage=1.0`, `projection_status=PARTIAL` |
| Read-only boundary | Redis `SMEMBERS/HGETALL` and TD `SELECT` only |

## Results

| Symbol | Opening fact | Transition | Reason |
|---|---|---|---|
| `000993` | `MATCH` | `MATCH` | 0925 change present |
| `300207` | `MATCH` | `NON_COMPARABLE` | `0925_chg_bp_missing_or_invalid` |
| `600330` | `MATCH` | `NON_COMPARABLE` | `0925_chg_bp_missing_or_invalid` |
| `600519` | `MATCH` | `MATCH` | 0925 change present |

Summary:

```text
opening_exact = true (4/4)
transition exact among comparable rows = true (2/2)
transition mismatches = 0
transition non-comparable = 2
```

The command-level `transition_exact` aggregate is not treated as a pass when
non-comparable rows exist. The two unavailable transition inputs remain
explicitly non-comparable; no value is fabricated from Q2 or from a nearby TD
row.

The real Q2 cohort was partial/stale at the observation time, but the selected
rows still produced identical Core and legacy opening facts. This proves
formula parity for the exercised fields only. It does not verify legacy
opening behavior thresholds, state lifecycle, or a production strategy rule.

## Reproduction

```text
cd /home/exedev/validation/engine-core-71f483a8c667e776cc7309d13d8c11cb4febe95e
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/run_opening_differential.py \
  --trade-date 2026-09-16 \
  --symbols 600519,600330,300207,000993 \
  --stale-after-ms 300000 \
  --legacy-root /home/exedev/services/engine-next/current \
  --output /tmp/core-opening-differential-20260916-71f483a.json
```

