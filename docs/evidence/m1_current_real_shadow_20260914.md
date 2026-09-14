# M1 current real shadow evidence (2026-09-14)

## Scope

This is a bounded, read-only observation of the current `cobra-ion` runtime
using the Core archive at commit `d08c7ce`.  It did not stop or restart
`engine-next`/`t1-v2-live`, add a Rabbit consumer, change ACK behavior, write
Redis/TDengine, repair data, or send a notification/effect.

Formal runtime: Cobra Python 3.12.3, `Asia/Shanghai`, locale `POSIX`.

## Real observations

### Redis Q2

Probe time: approximately `2026-09-14 11:28:12 CST`.

```text
trade_date                 2026-09-14
requested/received         5220 / 5220
coverage                   1.0
status                     STALE
consistency                BEST_EFFORT_STALE
stale symbols              5220
source time range          2026-09-13 16:00:00 ~ 2026-09-14 10:15:27 CST
newest source lag          about 1965 seconds
engine repeat hash         equal
read operations            SMEMBERS=1, HGETALL=5220
artifact SHA-256           efda31e27a1a66b389d6e5f2d5d0e94798e60894af97a7b66a5c3a6edea04543
```

The active cohort is real, but it is not fresh under the 60-second policy.
Coverage `1.0` is therefore not promoted to READY.

### TD previous-day reference

The real TD `daily_kline` path returned rows for `000001`, `300750`, and
`600519` on the calendar-derived previous trade date `2026-09-11`.

```text
rows                       3
requested/actual date      2026-09-14 / 2026-09-11
source                     tdengine_daily_kline
result                     UNAVAILABLE
reason                     available_at_unknown
read boundary              TD SELECT only
artifact SHA-256           9a2d955c74b1a7d5963d1908cbe11d808e7ff9a1221b07aa6cb9c5da3de6ab1b
```

The result is fail-closed: query observation time is not treated as historical
availability time.

### TD Auction projection and Core fact shadow

The real TD `auction_snapshot_v2` rows for `600519` contained all three
anchors: `0920`, `0924`, and `0925`.  Core rebuilt two adjacent business
segments with source timestamps preserved.  Both segments were `PARTIAL`
because the observed rows do not cover the whole business intervals.

```text
0920/0924/0925 source times  09:20:03.146 / 09:24:10.162 / 09:25:06.156 CST
fact status                  PARTIAL / FACT_ONLY / OBSERVE
comparison                   amount VOLUME_EXPANDING
                             pressure PRESSURE_WEAKENING
price                        UNKNOWN (projection price absent at 0920/0925)
artifact SHA-256             8ba0f567984146503157e5d1b04b24d83367fa3460ea1d1bd35f2540c1ccd96e
```

This proves the Core fact path against real TD projection rows, not Rabbit
arrival order, producer batch membership, or internal AuctionState timing.

### Redis/TD auction projection comparison

For `000001`, `000002`, and `600519`, all shared timestamp comparisons were
`MATCH`; no field mismatch was found.

```text
MATCH                 0
PARTIAL_COMPARABLE    5
NOT_COMPARABLE        4
MISMATCH              0
timestamp mismatch    0
artifact SHA-256      fac1dc39d17ccaa0b912bed3f49553e16806fc0d2c2d3b90e0395f19c9e7948d
```

`NOT_COMPARABLE` means the selected symbol was outside Redis `top_amount` or
the projection omitted a field; it is not a numeric mismatch.  Redis anchor
amount/bid fields that were present matched TD values exactly.  The comparison
does not prove the two writers consumed one identical internal AuctionState.

### Captured production-chain bundle

The existing ground-truth capture was replayed without synthesizing the empty
`0924` slot.  The recomputed manifest remains `PARTIAL`; the capture's
`formal_ground_truth=true` is not accepted as proof when a required slot is
failed.

```text
source_ingestion       UNKNOWN
auction_state          OBSERVED
storage_projection     WARN
engine-next            UNKNOWN
engine_core_q2         PASS (repeat hash equal)
engine_core_shadow     PARTIAL
joint                  WARN
audit_summary SHA-256  b2ff6004233c0d363339da7b1760324d86babcbd4a1290d75513283fe811ed30
```

The capture has no Rabbit/runtime batch membership or engine-next loader trace,
so `FIRST_DIVERGENCE` remains `UNPROVEN`.

## Current blocker and next action

Reference caches still lack `schema_version`, `available_at_ms`, and
`field_units`; current Core correctly leaves those results unavailable.  Do
not implement another Core abstraction to hide this.  The next authorized
change is an exact, controlled integration of metadata into the existing
writer, followed by a fresh read-only Cobra shadow.  Until that evidence exists,
Core remains a read-only shadow and `engine-next` remains the production owner.
