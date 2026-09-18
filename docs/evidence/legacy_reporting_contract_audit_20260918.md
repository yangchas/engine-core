# Legacy reporting contract audit (2026-09-18)

## Scope

This is a static, read-only audit of the deployed `engine-next` reporting path at
`/home/exedev/services/engine-next/releases/20260903_e272842`. It records the
old behavior that must be preserved or explicitly rejected before Core can
replace any reporting boundary. It does not modify the release, production
services, Redis, TDengine, RabbitMQ, SMTP, or webhook configuration.

Audited files and observed SHA-256 values:

| Area | File | SHA-256 |
|---|---|---|
| report build | `engine_next/runtime/auction_email_report.py` | `b33b173cad109e793bc9aa949cbe5c31a0ef3393876b8b89ec01af01c3807a33` |
| lifecycle/delivery gate | `engine_next/runtime/reporting_lifecycle.py` | `73b41002333a7563c156e10c620dbd80fb0a1d873f403f2d1cd5f6ff301c011a` |
| orchestration | `engine_next/runtime/production_reporting.py` | `658f37aa06e151b9565f23c5d620fd015907b34c6e699fb2a54a900a16c8dcab` |
| fact assembly | `engine_next/runtime/production_fact_assembly.py` | `3b145d6225031aacedf3a83f79bff139f51854b45809e469600cf6581180a2c8` |
| strategy/render controller | `engine_next/runtime/controllers/auction_runtime_controller.py` | `c7793bbc1b2f5dafc5c7303de72aa8411e9122d60468dbfa9912edaad23802a5` |

## Responsibility split

### 1. Fact assembly (read-only data boundary)

`production_fact_assembly.py` reads the deployed data views and assembles
auction/opening inputs. The observed path includes TD `auction_snapshot_v2`
rows for the 0920/0924/0925 tags, an explicit anchor universe, Redis summary
views, prior limit authority, and mapping data. It does not make the result a
Core fact merely because a row exists: missing tags, missing anchor universe,
and missing mapping remain explicit degradation states.

### 2. Report build (pure presentation boundary)

`auction_email_report.py` consumes already assembled facts/evidence/context and
builds deterministic HTML/text/Markdown output plus hashes. It validates report
format, trade date, and data origin, and distinguishes `COMPLETE`, `PARTIAL`,
and `DATA_UNAVAILABLE`. It keeps missing values visible as unavailable rather
than silently converting them to zero. The build path is not the SMTP/webhook
owner.

### 3. Lifecycle and delivery (external side-effect boundary)

`reporting_lifecycle.py` limits delivery to the allowed auction/opening events
and normal timing window. Recovery, manual, disabled, and replay paths fail
closed for production delivery. A Redis `setnx`-style claim/dedupe gate is
required before delivery; SMTP/webhook is owned by the external notification
service. Core must not acquire this ownership during shadow or replay.

### 4. Runtime controller and strategy console

`auction_runtime_controller.py` contains strategy-console rendering and many
thresholds/stateful business decisions. It is not a fact-only migration target.
Those rules require Gate B capability/rule/state-lifecycle parity before any
strategy rule is moved into Core.

## Core mapping as of this audit

| Legacy responsibility | Core status | Boundary |
|---|---|---|
| TD/Redis read-only fact inputs | present in bounded shadows | real source, explicit status and provenance |
| deterministic fact functions | present | no strategy conclusion in fact objects |
| frozen bundle/evidence lineage | present | no provider/network access from strategy |
| build-only report projection | implemented as `AuctionFactReportArtifact` (`69f8535`) | frozen fact-only projection; not a delivery owner |
| Redis delivery claim/dedupe | intentionally external | do not migrate into Core |
| SMTP/webhook/effects | intentionally external | deny-all in shadow/replay |
| full strategy-console controller | not migrated | requires Gate B parity |

## Contracts to preserve

1. Report origin is explicit. Current/cache, production capture, and replay
   fixture are not interchangeable without an explicit contract.
2. `Missing != Zero`; presentation defaults do not define fact readiness.
3. A partial local fact may be rendered as partial, but must not be upgraded to
   a complete market conclusion.
4. Build-only/report projection must not perform recovery, backfill, external
   network fetch, Redis/TD writes, claim, SMTP, or webhook calls.
5. Replay, recovery, disabled, and manual paths cannot trigger formal delivery.
6. A report hash identifies the canonical rendered/result payload; source
   evidence and lineage remain separately inspectable.

## Current parity matrix

| Legacy contract | Core projection | Status | Evidence |
|---|---|---|---|
| explicit accepted data origin | `data_origin` is validated against the fixed origin set | MATCH for the narrow fact artifact | `tests/test_reporting.py` |
| `COMPLETE/PARTIAL/DATA_UNAVAILABLE` report state | derived from `FactStatus`, never supplied by caller | MATCH for the narrow fact artifact | `tests/test_reporting.py` |
| missing values remain unavailable | metrics preserve `None` and status remains degraded | MATCH for the narrow fact artifact | `tests/test_reporting.py` |
| deterministic business hash separate from evidence | `semantic_hash` and `evidence_hash` are independent | MATCH for the narrow fact artifact | `tests/test_reporting.py` |
| A2 market summary authority | not represented by `AuctionFactShadow` | UNKNOWN / not migrated | requires a verified A2 fact source |
| plate rows, locked-order tables, appendix rankings | not represented by the current single-symbol fact shadow | NOT_APPLICABLE to this first slice | requires Gate B plate capability parity |
| Redis claim/dedupe before delivery | intentionally outside Core | INTENTIONAL_CHANGE | delivery remains `engine-next` owner |
| SMTP/Webhook notification | intentionally outside Core | INTENTIONAL_CHANGE | no notifier imported or called |

The matrix is capability-local. It does not claim that the narrow fact report
is equivalent to the old full email report. Full report parity remains blocked
until the A2 summary, plate universe, locked orders, mapping, and lifecycle
contracts have verified authorities.

## Test evidence available in the old tree

The old tests provide an offline parity inventory, not a production proof. The
relevant groups include:

- `auction_email_report_checks.py`: deterministic/read-only build, origin
  acceptance, report status, A2 authority, missing-vs-zero, degraded detail,
  no strategy phrases, and no notifier side effects.
- `production_fact_assembly_checks.py`: TD row normalization, real TD
  datetime, Redis+TD assembly, mapping/anchor/0920-0925 missing behavior,
  zero activity, and unavailable report inputs.
- `production_reporting_runtime_checks.py`: mapping readiness/hash, Q2
  fallback, exception-before-claim/notification, dedupe, recovery/disabled
  behavior, build-only mode, and notifier-unavailable partial status.

These tests are useful for selecting the first pure Core projection, but they
do not prove current production source freshness, batch membership, or
engine-next loader parity.

## Current decision

`ENGINE_CORE_REPORT_OWNER = NOT_READY`.

The next safe implementation after `AuctionFactReportArtifact` is capability-
local parity for one verified report field at a time. It must not include a
notifier, Redis claim, SMTP/webhook call, implicit recovery, or strategy
thresholds. Live acceptance remains blocked while t1-v2 reports TD storage
`No enough disk space` and the real Q2 projection is stale.

This audit is therefore an offline migration input, not a production readiness
claim.
