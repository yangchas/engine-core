# Morning Vertical Slice Shadow Evidence — 2026-09-14

## Scope

This evidence records a bounded, read-only composition run for the Core morning
vertical slice. It is not production replacement validation and it does not
prove the `engine-next` loader/report path.

The runner composes:

```text
Q2 Adapter
  -> Current Q2 projection
  -> existing auction shadow facts from TD `auction_snapshot_v2`
  -> opening transition fact
  -> Core-owned timer evidence (`AUCTION_0926`, `OPENING_0932`)
```

It does not consume Rabbit, ACK messages, write Redis/TD, recover data, send
mail/notifications, or execute a strategy/effect.

## Exact code and environment

```text
Core commit: b902d7e99f1294dcffee275a15d17818ab7ae900
Archive SHA-256: 99d635c859e96782dab097609b1e30de0f30ae140dca15dcea0b0578f1744114
Remote Python: 3.12.3 (`shared/venv/bin/python`)
Local tests: 332 passed
Cobra tests: 332 passed
compileall: PASS (local and Cobra)
```

The Cobra run used an isolated extraction directory under
`/home/exedev/validation/engine-core-b902d7e/`; no production files were
overwritten.

## Fixed-time deterministic run

Input:

```text
trade_date: 2026-09-14
symbol: 600519
Q2: captured q2_093210.jsonl, observed_at=09:31:20 Asia/Shanghai
TD: read-only auction_snapshot_v2 query for 0920/0924/0925
current timer time: 09:33:00 Asia/Shanghai
```

The runner was executed twice against the same captured Q2 and the same
read-only TD query. Both output artifacts had the same SHA-256:

```text
fd26f508faaca7fda1741d34d08f5af6a2f72e14c776d6966a62c45b35532e35
```

Both semantic hashes were:

```text
312a72b3b7bf9e57d06004cb1963014ce68a14ad760b10a1914a3189a3728051
```

Relevant component hashes:

```text
Q2 content hash:       5f30cad1dc9654f08b2cf9b818bb076ce55066edbd1ffda605270640fcfa6980
Auction shadow hash:   0df98bbaa5072f03309075a1c9e3484f115aaeef60d5300bb2afa7b6e29f9aa6
```

## Real live read-only run

At approximately 14:51 CST, the runner read the current Redis Q2 projection
and the TD auction rows for 600519. The production services remained active
with unchanged PIDs and zero restarts.

Observed result:

```text
Q2: 5220/5220 symbols, coverage=1.0, status=STALE under the 300s policy
TD auction rows: 3 (0920/0924/0925)
Auction shadow: PARTIAL / FACT_ONLY / OBSERVE
Auction changes: amount expanding; order-book pressure weakening;
                 price unavailable; breadth/theme unavailable
Opening transition: unavailable (0925 change field unavailable)
Previous-day stats: UNAVAILABLE (historical available_at unknown)
Hot plates:         UNAVAILABLE (metadata contract not verified)
```

The Q2 source-time range and stale-symbol list were retained. `coverage=1.0`
was not promoted to READY; freshness/completeness remained separate.

## Acceptance interpretation

```text
Core composition and deterministic repeatability: PASS
Real Redis Q2 read path:                       PASS (read-only, stale/partial)
Real TD auction evidence path:                 OBSERVED
Reference-data readiness:                      NOT CLOSED
Engine timer consumption:                      NOT PROVEN by this runner
engine-next loader/report parity:              NOT PROVEN
Core replacement readiness:                    NOT READY
```

The 0924 Redis capture gap and the lack of historical `available_at` evidence
remain explicit limitations. No 0920→0924 segment is synthesized when an
anchor is missing; non-adjacent 0920→0925 data is not treated as an adjacent
comparison.

## Production safety

```text
new Rabbit consumer: 0
Rabbit ACK changes:  0
Redis writes:        0
TD writes:           0
notifications/effects/orders: 0
engine-next restart: 0
t1-v2-live restart:  0
```

## Next bounded step

Stop expanding the foundation. The next step is Gate B for the first verified
Auction Shadow rule, using the existing facts and this evidence boundary. Core
must not be declared a replacement until startup readiness, key-node behavior,
report ownership, and multi-day shadow differential evidence are complete.
