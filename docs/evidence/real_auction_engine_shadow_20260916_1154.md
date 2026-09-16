# Real auction projection through Engine — 2026-09-16 11:54 CST

## Scope

One bounded, single-symbol, read-only run used the existing TD
`auction_snapshot_v2` SELECT path and the public Core signal queue. It is an
integration proof for the fact-only path, not a strategy migration or a
production replacement test.

```text
TD SELECT
  → canonical auction projection
  → MARKET_UPDATE + TIMER per anchor
  → DeterministicEngine
  → AuctionShadowStrategy
  → AuctionFactShadow
```

## Result

```text
core archive: engine-core-3c5be9c7c406623de53ee7c5b9532cffac95f860
trade_date: 2026-09-16
symbol: 600519
processed_signals: 6
strategy_result_count: 3
engine_fact_status: PARTIAL
engine_fact_only: true
semantic_hash_equal: true
direct_fact_content_hash: 8a89c528335a30b78efc47926c05cd34bc5466a056fb9d7034fefae934557d2a
```

Source record times were preserved without rewriting:

```text
0920: 1789521603122
0924: 1789521850201
0925: 1789521906097
```

The direct pure-wheel and Engine-composed semantic hashes matched. Evidence
hashes are intentionally different because the Engine adapter has its own
snapshot identity; this is not a semantic mismatch.

## Safety

```text
read_only: true
side_effect_boundary: TD SELECT + in-memory Engine only
Rabbit consumer/ACK changes: 0
Redis writes: 0
TD writes: 0
notification/effect: 0
```

The fact remains `PARTIAL` and `FACT_ONLY`; no threshold, candidate, BUY/PASS,
report send, or replacement claim is emitted.

