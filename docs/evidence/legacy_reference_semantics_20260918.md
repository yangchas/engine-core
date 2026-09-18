# Legacy reference-data semantics audit — 2026-09-18

## Scope

Read-only comparison of the deployed `engine-next` release and the live
Redis projections used by the Core reference functions. No production service,
Rabbit consumer, Redis key, or TD table was modified.

## Evidence

| Item | Evidence |
|---|---|
| Legacy producer | `/home/exedev/services/engine-next/releases/20260903_e272842/ai/API/StockAnalyzer.py`, SHA-256 `f97dc263ec2bb9126a19a58a22ddb61e8bb5aa074e8788c13c3358fe4b4c2d10` |
| Legacy connector | `/home/exedev/services/engine-next/releases/20260903_e272842/engine_next/connectors/kaipan_connector.py`, SHA-256 `a47cf8d83c7b4c6361345e498e39231a949bec2c83906375606ddd2810c23119` |
| Live probe | `/home/exedev/validation/engine-core-6511981-v1/reference_readiness_live_cutoff_20260918_1355.json`, SHA-256 `da8c7530c5d6c195de5e5648d11c58b2363f381e0cb8c4929eb3248283b80960` |
| Redis limit-pool bucket | `cache:yest_limit_pool:2026-09-17`, 47/47 HSCAN rows, stable HLEN, source `kaipan` |
| Redis hot-plate bucket | `cache:hot_plates:2026-09-18`, 50/50 HSCAN rows, stable HLEN, source `kaipan` |

## Canonical mapping decisions

### Yesterday limit pool

The deployed `StockAnalyzer.get_history_bans_pool()` maps the Kaipan record as:

```text
rec[0]  -> code -> six-digit symbol
rec[2]  -> close_pct
rec[3]  -> seal_time
rec[9]  -> turnover
rec[12] -> plate
rec[15] -> lb_days
```

The live Redis values for the legacy `turnover` field are amount-sized (for
example `61,814,324`, `101,359,868`, and `1,525,498,224`), not percentage
points. The Core provider boundary therefore maps:

```text
legacy raw field `turnover` -> canonical `turnover_yuan`
close_pct                         -> canonical `close_pct` (percentage points)
```

No scaling is applied. This is a semantic rename, not a numeric conversion.
The old unitless field name must not be exposed by Core facts or strategies.

The live Redis metadata still does not contain `schema_version`,
`field_units`, or historical `available_at_ms`. Therefore this audit closes
the value-unit question for the current legacy producer, but does not make
historical replay available. Historical/Replay remains fail-closed without
verified `available_at_ms`; LIVE may use the already-approved fetch-completion
policy when an explicit node cutoff is supplied.

### Hot plates

The deployed connector maps tuple payloads as:

```text
tuple[1] -> plate_name
tuple[2] -> strength / hot candidate
tuple[3] -> change_pct
tuple[6] / 1e8 -> net_inflow_yi
```

`change_pct` and the `1e8` conversion for `net_inflow_yi` are source-formula
evidence. However, live Redis values (`strength`/`hot` approximately 89–153 on
2026-09-18) do not match the old consumer's `strength >= 3000` thresholds.
This is a producer/API generation or scale mismatch until proven otherwise.

```text
strength: UNKNOWN / NOT_AUTHORIZED for strategy thresholds
hot:      UNKNOWN / NOT_AUTHORIZED for strategy thresholds
net_inflow_yi: source-formula observed, consumer parity still pending
```

Core must keep these fields unavailable for strategy use and must not apply a
guessed rescaling. A separate source-schema investigation is required before
any hot-strength threshold is migrated.

## Parity status

| Capability | Status | Reason |
|---|---|---|
| Raw limit-pool field mapping | MATCH | Deployed producer source and live values agree |
| `turnover_yuan` unit | MATCH | Amount-sized live values and producer index agree |
| `close_pct` percentage-point unit | MATCH | Producer uses `rec[2]`; live values are around 10.0 |
| Limit-pool historical availability | UNKNOWN | Legacy Redis metadata has no historical availability proof |
| Hot `change_pct` | OBSERVED | Connector maps source field; runtime consumer parity still pending |
| Hot `net_inflow_yi` | OBSERVED | Connector explicitly divides tuple[6] by `1e8` |
| Hot `strength`/`hot` scale | UNKNOWN | Live values conflict with legacy threshold scale |

## Core guardrails

- `PreviousDayLimitPoolFunction` exposes `turnover_yuan`, never unitless
  `turnover`.
- Missing historical availability remains `UNAVAILABLE` in HISTORICAL/REPLAY.
- LIVE readiness still requires a real adapter completion timestamp at or
  before the explicit node cutoff.
- Hot strength/heat fields remain `UNAVAILABLE` for strategy consumption until
  a matching source schema and consumer oracle are proven.

