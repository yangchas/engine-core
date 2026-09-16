# Real auction projection through Engine — 2026-09-16

## Scope

This evidence closes one narrow integration gap: a real
`auction_snapshot_v2` projection can traverse the public Core signal path and
produce the same fact semantic hash as the already verified pure wheel.

It does **not** claim full auction strategy migration, full-market capacity,
Rabbit arrival/batch equivalence, report ownership, or replacement of
`engine-next`.

## Path

```text
TD SELECT (three bounded rows)
  -> auction projection adapter
  -> MARKET_UPDATE
  -> TIMER
  -> DeterministicEngine
  -> AuctionShadowStrategy
  -> AuctionFactShadow
```

The validation is single-symbol and read-only. It does not consume Rabbit,
change ACKs, write Redis/TD, invoke recovery, send notifications, or trigger
effects.

## Cobra-ion result

```text
core commit: ee2580addb48968cc23cde0cee7c873d3d1832b2
Python: 3.12.3
trade_date: 2026-09-16
symbol: 600519
processed signals: 6
strategy results: 3
engine fact status: PARTIAL
engine fact only: true
semantic_hash_equal: true
```

The source timestamps were preserved for all three anchors. The engine fact
hash was:

```text
8a89c528335a30b78efc47926c05cd34bc5466a056fb9d7034fefae934557d2a
```

The direct pure-wheel hash was identical. Evidence hashes differ because the
Engine adapter uses Engine-generated snapshot identities; this is expected and
does not imply a semantic mismatch.

The same isolated archive passed `413 passed` and `compileall` on Cobra-ion.
