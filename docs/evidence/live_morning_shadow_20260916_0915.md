# Cobra-ion 2026-09-16 normal-origin Core morning shadow

## Scope

This is a bounded, read-only execution of the existing `LiveMorningShadowV1`
runner. `engine-next` and `t1-v2-live` remained the production owners. The
runner did not consume Rabbit, acknowledge or publish messages, write Redis or
TDengine, recover data, send notifications, or execute effects.

Remote output directory (write-once):

```text
/home/exedev/validation/live-morning-shadow-20260916-0915
```

The runner started at `2026-09-16T09:14:50.350489+08:00` and emitted the two
Core-owned normal-origin nodes at their actual due times:

| Node | Actual observation | Late by | File SHA-256 |
| --- | --- | ---: | --- |
| `AUCTION_0926` | `2026-09-16T09:26:00.067561+08:00` | 67 ms | `a6a69361395db9972914d4a69b6e5a88e9725da229351b86319ba50e1ceb369b` |
| `OPENING_0932` | `2026-09-16T09:32:00.119303+08:00` | 119 ms | `83682ec4cd97d5c9f7baa75b06ef9c5bf11f2bd2a6d2b881fe3beaf6cbb88eec` |

Manifest SHA-256:

```text
818275892d0e1829ec73080734c9629bf73df75b671a1d57b7130770fbce9678
```

Startup SHA-256:

```text
e10e7557a8ff4cb4c8d1481aa0b1eaea0bf83acdb393de2e1393b573dc76525e
```

## Observed results

At startup (`09:14:50`) Redis Q2 was not yet populated for the requested
trade date: `MISSING`, `coverage=0.0`, `consistency=EMPTY_UNIVERSE`. Readiness
correctly returned `BLOCKED/WAIT_FOR_Q2` and did not call a provider or write
anything.

At `AUCTION_0926`, the bounded TD read returned three rows per selected symbol
(`000001`, `000002`, `600519`). The existing Core `AnchorDeltaFactV1` path
produced `READY` facts for the `0924 -> 0925` transition. The legacy Redis
loader was also read-only and observed 200-row Top-N projections for each of
`0920`, `0924`, and `0925`; this is not full-universe proof.

At `OPENING_0932`, Redis Q2 returned `5221/5221` symbols with `coverage=1.0`
but `status=PARTIAL` and `consistency=BEST_EFFORT_PARTIAL`. The source-time
range was preserved (`oldest_source_time_ms=1789401600000`,
`newest_source_time_ms=1789522266000`), so coverage was not promoted to
freshness. The three selected `OpeningTransitionFactV1` results were
`READY`; this is a bounded fact shadow, not a production decision.

The runner manifest reported:

```text
origin=NORMAL
node_count=2
new_rabbit_consumer=0
rabbit_ack_or_publish=0
redis_write=0
td_write=0
notification_or_effect=0
production_restart=0
```

## Acceptance interpretation

| Area | Result | Meaning |
| --- | --- | --- |
| Timer normal-origin due/firing | PASS | `AUCTION_0926` and `OPENING_0932` fired once at their business deadlines. |
| Source-time preservation | PASS | Business anchor time and observed/source times remain separate. |
| Auction fact dispatch | PASS | Existing verified anchor-delta fact wheel was used; no strategy conclusion was added. |
| Opening fact dispatch | PASS (bounded) | Selected symbols produced opening facts while preserving batch `PARTIAL`. |
| Startup Q2 readiness | PASS (fail-closed) | Missing Q2 produced `WAIT_FOR_Q2`, not fabricated readiness. |
| Full replacement readiness | NOT READY | No durable restart identity, Rabbit batch membership, writer same-state proof, formal strategy/report owner, or multi-day equivalence. |

## Identity limitation

The remote runner output does not record the exact engine_core commit/build
identity. Therefore this artifact is **real server evidence for the runner
behavior**, but it is not an exact current-HEAD code verification record.
Future live shadows must record `commit_sha` (or an immutable build id) in the
manifest before execution.

## Next bounded action

Do not add a new strategy or infrastructure layer. First close the M1
lifecycle evidence with a build-identified run and the existing wheels:

1. record immutable build identity in the runner manifest;
2. repeat the normal-origin startup/readiness and `0926/0932` nodes on the next
   trading day;
3. add the already verified `AnchorDeltaFactV1` and `OpeningFactV1` hashes to
   the node evidence;
4. keep missing/stale Q2 and unknown reference availability fail-closed;
5. only after M1 lifecycle evidence is complete, audit one legacy strategy rule.

