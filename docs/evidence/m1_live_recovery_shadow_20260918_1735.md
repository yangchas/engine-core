# M1 Real Recovery-Catchup Shadow — 2026-09-18 17:35 CST

## Scope

The exact isolated Cobra-ion validation copy ran the existing bounded
`run_live_morning_shadow.py` against real Redis Q2 and TD read-only paths for
`600519`. The run was deliberately after the market session, so it is
`RECOVERY_CATCHUP` evidence, not normal 09:20/09:25/09:32 timing evidence.

```text
trade_date       2026-09-18
observation      17:35 CST
symbols          600519
node_count       2
nodes            AUCTION_0926, OPENING_0932
startup          PARTIAL / Q2 STALE / coverage 1.0
```

The new startup trace was present in `startup.json`:

```text
STARTUP_0830  RECOVERY_CATCHUP / PARTIAL / STALE
STARTUP_0900  RECOVERY_CATCHUP / PARTIAL / STALE
```

The auction node retained its independent TD fact path. The opening node
retained the Q2 input gate and reported `PARTIAL` rather than treating the
stale Q2 cohort as a normal opening snapshot.

## Safety

Manifest safety counters were all zero:

```text
new_rabbit_consumer       0
rabbit_ack_or_publish     0
redis_write               0
td_write                  0
notification_or_effect    0
production_restart        0
```

`engine-next` and `t1-v2-live` remained production owners and were not
restarted.

## Evidence

Remote directory:

```text
/home/exedev/validation/m1-live-shadow-20260918-1735
```

```text
startup.json       a7e88045cd3c01c4c790bc1e6f2cf45e4763d53e575c8deda20071080579108a
AUCTION_0926.json  bc275bbc7a8e1f732d3b34fe900eb6db50e176284f41fc7883e89693b04fc7ec
OPENING_0932.json  7fccd43fa6ed70b5d9662abf3bfe74b62e0b9cea2008512ba15e0aa7dc7823c9
manifest.json      6dfa8b8cc583e993573675a78e2df173d05165465620937dc82329ebf61fdb28
```

The source-tree identity in the manifest is
`160baf9d2721241f7d3389c74c21ee023383407fc9e7c2eeebd91b2c91bc268a`, matching
the local source-tree identity for the same code.

This closes only the real post-market recovery-shadow composition. It does
not close normal-origin startup, source freeze ownership, fresh-Q2 admission,
reference-data prefetch at 09:20, or Core replacement acceptance.
