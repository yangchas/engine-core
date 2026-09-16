# Legacy opening behavior audit — 2026-09-16

## Scope

This evidence records a bounded, read-only audit of the deployed
`engine_next` opening context and its existing opening-behavior classifier.
It is not a Core strategy migration and does not promote any legacy threshold
to a verified rule.

## Runtime evidence

| Item | Value |
|---|---|
| Server | `cobra-ion` |
| Trade date | `2026-09-16` |
| Legacy root | `/home/exedev/services/engine-next/releases/20260903_e272842` |
| Input | Real Redis Q2 through the legacy `IntradayDataHub` path |
| Symbols | `000993, 300207, 600330, 600519` |
| Probe contract | `EngineNextContextProbeV2` |
| Engine Core commit | `d17d153c053730f98897f90a88e3b32514aa0ff0` |
| Read-only guard | `guard_writes=[]` |
| Probe observed at | `2026-09-16T03:28:19.505423+00:00` |
| Latest source timestamp | `1789527513000` |
| Future source timestamp | `false` |

The probe disabled legacy cache, network, recovery, writer, notification and
effect hooks. It performed real Redis reads and called only the existing pure
legacy fact/classification functions. No Rabbit consumer, ACK, Redis write or
TD write was introduced.

The same commit passed the local and cobra-ion verification suites:

```text
local:     418 passed; compileall PASS
cobra-ion: 418 passed; compileall PASS; real probe PASS
```

## Observed legacy context and behavior

Amounts are the legacy context's Yuan values. Percentage fields retain the
legacy ratio representation (for example `0.10` means approximately 10%).
The amount floor is calculated from the full context using the existing
`relative_amount_floor(top_n=160, fallback=20_000_000)` helper and is only
reported as audit evidence.

| Symbol | Open ratio | Current ratio | Auction amount (Yuan) | Amount 2m (Yuan) | Speed 1m (ratio) | Floor (Yuan) | Legacy label |
|---|---:|---:|---:|---:|---:|---:|---|
| `000993` | 0.0250 | 0.100000 | 89,184,096 | 2,047,488 | 0.0000 | 5,000,000 | `limit_attack` |
| `300207` | 0.154453 | 0.154453 | 9,534,195 | 69,256,576 | -0.0024 | 5,000,000 | `limit_attack` |
| `600330` | 0.0000 | 0.043059 | 9,059,430 | 6,907,648 | -0.0019 | 5,000,000 | `low_open_repair` |
| `600519` | 0.0009 | -0.013003 | 9,427,100 | 18,591,616 | 0.0000 | 5,000,000 | `mixed` |

## Interpretation boundary

`opening_behavior_audit.status=OBSERVED`, while `rule_status=UNKNOWN`.
The output demonstrates that the old runtime can assemble real Q2 context and
produce deterministic labels for the selected symbols. It does **not** prove
that the labels are suitable for Core migration because the following remain
unclosed:

- real positive/negative/partial legacy oracle coverage;
- amount and `speed_1m` semantic parity across Q2 and Core inputs;
- state lifecycle and cross-session reset behavior;
- threshold ownership and business acceptance criteria.

The next migration step remains a fact/legacy differential audit, not a new
Core strategy implementation.

## Reproduction

```text
cd /home/exedev/validation/engine-core-d17d153c053730f98897f90a88e3b32514aa0ff0
/home/exedev/services/engine-next/shared/venv/bin/python \
  examples/run_engine_next_context_probe.py \
  --legacy-root /home/exedev/services/engine-next/current \
  --trade-date 2026-09-16 \
  --previous-trade-date 2026-09-15 \
  --symbols 600519,600330,300207,000993 \
  --now <server-local-time> \
  --output /tmp/engine-next-context-20260916-opening-audit.json
```
