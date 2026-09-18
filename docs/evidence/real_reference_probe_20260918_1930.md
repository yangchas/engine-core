# Real Reference Source Probe — 2026-09-18 19:30

## Scope and safety

This was a bounded, post-market, read-only connectivity probe through the verified
legacy connector path. It did not import Core into the legacy runtime, write
Redis/TD, consume or acknowledge Rabbit messages, repair caches, send notifications,
or assemble strategy/effect components.

- Host: `cobra-ion`
- Legacy release: `e272842c8f490f55a1b017badb71e71904ce008e`
- Runtime: engine-next shared Python 3.12.3 environment
- Artifact: `/home/exedev/validation/real-reference-probe-20260918-1930.json`
- Artifact SHA-256: `b3506114b96f260bb1523b2e095471fbe37fb66b75cf835a4d24cc36d32c4870`

## Observed results

| Source | Connection | Contract result | Temporal interpretation |
|---|---:|---:|---|
| BaoStock daily kline | PASS | PASS | Explicit request/response date `2026-09-18`; safe for this bounded live observation |
| Kaipan ban reasons | PASS | OBSERVED | No returned source trade date; not historical runtime input |
| Kaipan hot plates | PASS | OBSERVED | Requested date was passed, but response is not self-dated; units/availability remain unclosed |
| Kaipan yesterday limit pool | PASS | MISSING | Empty result for this probe; no fallback or fabricated rows |
| THS hot rank | PASS | OBSERVED | Current query has no date parameter; historical runtime role `UNAVAILABLE` |
| Wencai limit truth | PASS | OBSERVED | Current query has no structured date; historical runtime role `UNAVAILABLE_WITHOUT_DATED_QUERY_EVIDENCE` |

All six connector calls reached their source path, but only the BaoStock daily-kline
call closed both requested and returned date semantics. The other results remain
evidence/oracle material only and must not be promoted into replay or historical
`FrozenDataBundle` inputs without verified `available_at`/date semantics.

## Core migration implication

The existing connectivity is reusable, but the Core boundary must continue to be:

```text
legacy connector access
→ thin ProviderResult
→ DataFunction semantic contract
→ TemporalDataGuard
→ FrozenDataBundle
```

This probe does not authorize a new Provider hierarchy, fallback semantics, or a
production replacement. It closes only the current real-connectivity observation.

