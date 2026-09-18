# Legacy report status parity (2026-09-18)

## Scope

This evidence closes one capability-local boundary only: the presentation
status mapping used by the old `engine-next` auction report and the Core
build-only fact report. It does **not** claim full email/report parity, plate
rows, locked-order tables, delivery claims, SMTP/webhook effects, or strategy
console parity.

## Legacy evidence

The deployed `engine-next` report build accepts the same three externally
visible report states used by the reporting contract:

```text
COMPLETE
PARTIAL
DATA_UNAVAILABLE
```

The old report source is recorded in
`docs/evidence/legacy_reporting_contract_audit_20260918.md` with its deployed
release SHA-256. Missing optional inputs are rendered as unavailable and are
not silently promoted to a complete report.

## Core contract

`engine_core.reporting._report_status` maps the frozen fact status as follows:

| Frozen fact status | Core report status |
|---|---|
| `READY` | `COMPLETE` |
| `PARTIAL` | `PARTIAL` |
| `MISSING` | `DATA_UNAVAILABLE` |
| `INVALID` | `DATA_UNAVAILABLE` |
| `UNAVAILABLE` | `DATA_UNAVAILABLE` |

The mapping is deterministic and is derived from the fact status; callers
cannot supply a report status independently. The new parametrized test
`test_report_status_mapping_matches_legacy_three_state_contract` covers every
`FactStatus` value.

## Result

```text
LEGACY_REPORT_STATUS_PARITY = MATCH
SCOPE = status boundary only
FULL_REPORT_OWNER = NOT_READY
DELIVERY_OWNER = engine-next/external lifecycle
```

No Redis/TD writes, network recovery, Rabbit changes, notification, or
production service changes were made.
