# Live node-boundary readiness shadow — 2026-09-16 12:38 CST

The exact `c4b0ac9` validation archive was run on cobra-ion in a new,
write-once directory:

```text
/home/exedev/validation/live-morning-node-recheck-20260916-1238
```

This was a late-start, `RECOVERY_CATCHUP` evidence run. It is not a claim
that the 09:26/09:32 production moments were replayed exactly.

## Real read-only path

```text
Redis Q2 (SMEMBERS/HGETALL)
    → node-boundary readiness recheck
TD stock/auction SELECT
    → AnchorDeltaFactV1 / OpeningTransitionFactV1
```

The Q2 recheck was performed once for each Core node. The auction node kept
its independent TD fact path; the opening node used the same node-boundary Q2
observation for its opening fact.

## Observed result

```text
AUCTION_0926:  RECOVERY_CATCHUP, Q2 PARTIAL, coverage 1.0,
               TD rows 3, AnchorDeltaFactV1 READY
OPENING_0932:  RECOVERY_CATCHUP, Q2 PARTIAL, coverage 1.0,
               TD rows 3, OpeningTransitionFactV1 READY
```

The Q2 cohort was complete by row count but not fresh under the explicit
60-second policy, so readiness remained `PARTIAL` with `REFRESH_Q2`. The
source-time range was preserved (`oldest=1789401600000`,
`newest=1789529402000`); no source timestamp was rewritten to the timer.

## Evidence hashes

```text
AUCTION_0926.json: db09561c09b35e618c89c73c72266e0a9cf73fd7caac3c6ef8d956573c0b2095
OPENING_0932.json: 455e3cb9932e73ea6d69230668db07069f586dafead29713abdd56b721e7bc21
manifest.json:     8bc8753cf031b42e7bae905d3d259dea128d671a040a78f8b580c8517611e38d
startup.json:      a338f351d73c952944744af99b65a23c3f0717b57ed7b037b980c4396313b6db
```

The run used cobra-ion Python 3.12.3 and the production read-only TD/Redis
client paths. It performed no Rabbit consumption or ACK, no Redis/TD write,
no recovery/backfill, no notification/effect, and no service restart.

## Scope boundary

This closes a real node-boundary readiness observation for the updated shadow
runner. It does not prove in-session 09:26/09:32 timing, Rabbit batch
membership, t1-v2 freeze ownership, historical reference-data availability,
or Core replacement of `engine-next`.
