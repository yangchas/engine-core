# M1 startup replacement gap audit — 2026-09-18

## Scope

This is a capability comparison between the deployed `engine-next` startup path
and the current `engine_core` read-only runtime. It is an audit, not a claim of
replacement parity. No remote service or production data was changed.

## Authority comparison

| Capability | `engine-next` current owner | Core current state | Status |
|---|---|---|---|
| Trade-date/calendar validation | `RuntimeStartupCoordinator` + legacy calendar | `TradingCalendarSnapshot` + `SessionRuntimeCoordinator` | `PASS` for pure validation |
| Previous trade-date derivation | legacy startup request/context | `TradingCalendarSnapshot.previous_trade_day` | `PASS` for date authority |
| Q2 read and freshness/coverage | runtime Redis/Q2 path | read-only Q2 adapter and readiness assessment | `PASS/WARN`, real source can be stale |
| 08:30/09:00 checkpoint identity | legacy startup loop | pure timer/checkpoint trace | `SHADOW_ONLY` |
| Daily kline/factor/chip/DDE gap audit | offline sync executor and startup bootstrap | no equivalent acquisition/repair owner | `OPEN` |
| Yesterday limit pool/hot plates/plate mapping | Redis/Kaipan startup repair path | read-only reference functions/probes only | `OPEN` |
| Auction anchor recovery | legacy recovery path | deliberately not implemented in Core | `OPEN` |
| Persistent startup state | legacy cache/checkpoint lifecycle | in-memory/read-only evidence only | `OPEN` |
| Email/notification/effect | legacy owner | Core deny-all/build-only | `INTENTIONAL_DEFER` |

## What is actually proven

Core can validate an already-observed trading date, calculate due timers, apply
Q2/reference temporal gates, and produce deterministic read-only shadow traces.
The real Redis Q2 path has been exercised, but the observed production Q2 was
stale. The current TD write path also has repeated `No enough disk space`
errors, so TD-dependent startup parity is not admissible as a complete proof.

## Replacement gap

The missing capability is not another generic Engine abstraction. It is a
bounded startup coordinator that can, using the already verified provider
paths:

1. perform the startup self-check;
2. derive the previous trade date from the calendar authority;
3. prefetch and freeze the reference data needed before a node deadline;
4. preserve explicit `READY/PARTIAL/UNAVAILABLE` outcomes;
5. dispatch only read-only Core nodes when their inputs are safe.

It must not perform repair, persistence, Redis/TD writes, Rabbit actions,
notifications, or effects until a separate migration gate authorizes those
owners. This work can proceed as a shadow composition, but replacement
acceptance remains blocked by the real TD storage incident and missing legacy
writer/repair ownership.

## Next implementation gate

Do not add another replay or provider family. The next bounded migration unit is
the existing-provider, read-only startup composition for one target date and a
small symbol set. Its acceptance requires:

```text
calendar authority                PASS
target trade-date validation      PASS
Q2 observation and source range    PASS/WARN (truthful)
reference prefetch before cutoff  PASS or explicit UNAVAILABLE
single read-only node dispatch    PASS
no repair/write/effect            PASS
TD storage health                 CLEAR before TD parity is claimed
```
