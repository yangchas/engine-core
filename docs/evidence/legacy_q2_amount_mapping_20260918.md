# Legacy Q2 amount mapping evidence — 2026-09-18

## Scope

This is a bounded, read-only comparison from the real Cobra-ion probe artifact
`tmp/real-reference-20260918/legacy-context-probe-1645.json`. It checks only
the raw Q2 `am` field mapping. It does **not** claim that Redis Q2 and the
legacy auction projection are the same source or that the legacy plate rules
are migrated.

## Observed rows

| symbol | Core `px/pc-1` | legacy `current_pct` | Core `am` | legacy `auction_amount` | result |
|---|---:|---:|---:|---:|---|
| 000001 | 0.007751937984496138 | 0.007751937984496138 | 4,333,500 | 4,333,500 | MATCH |
| 000002 | 0.0993377483443707 | 0.0993377483443707 | 696,600 | 696,600 | MATCH |
| 600519 | -0.007782285434655756 | -0.007782285434655756 | 14,312,200 | 14,271,787 | SOURCE_PRIORITY_DIVERGENCE |

The first two rows support the field-level mapping:

```text
raw Q2 am (yuan)
→ Q2Quote.auction_amount_yuan
```

All three bounded rows also match the legacy current-price ratio formula:

```text
Q2 price_milli / pre_close_milli - 1
→ legacy current_pct ratio
```

This is a formula/field parity result only; it does not authorize any legacy
threshold or plate classification.

The 600519 difference is retained rather than normalized away. It is evidence
that the old context path can use a frozen auction projection which is not the
current Redis Q2 `am` value. Therefore this closes only the raw field mapping;
it does not close `engine-next` source-priority or full auction projection
parity.

## Safety and status

- Redis operation: read-only `HGETALL`; no writes.
- Legacy probe: pure context/fact path under write guards; `guard_writes=[]`.
- `source_record_time_ms` is preserved as observed source time and is not
  interpreted as Rabbit arrival time or exchange tick order.
- Legacy plate strings and expectation labels remain observation-only.
- Status: `VERIFIED` for raw `am` parsing and unit naming; `UNKNOWN` for
  projection authority/source priority beyond this bounded sample.
