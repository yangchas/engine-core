# Real Redis read-only shadow — 2026-09-18

## Runtime boundary

- Server: `cobra-ion`
- Code commit: `ed0ff38df12c5fb0a004b17a216d9b340c9dc443`
- Python: `/home/exedev/services/engine-next/shared/venv/bin/python` 3.12.3
- `engine-next`: active
- `t1-v2-live`: active
- No Rabbit consumer was added, no ACK behavior changed, and no Redis/TD write
  was performed.

## Theme-delta strategy shadow

The runner read only the existing Redis auction projections and mapping hashes.
It did not use TD, fallback repair, Rabbit, notifications, or effects.

```text
status                 OBSERVED
trade_date             2026-09-18
row_count              175
mapping_count          170
mapping_missing_count  5
signal_counts          增量转强=3, 封单增强=4, 平稳=2, 温和放量=110
```

Artifact:

```text
/home/exedev/validation/engine-core-ed0ff38df12c5fb0a004b17a216d9b340c9dc443/live-20260918/theme-delta-strategy.json
sha256=db2f609897a91d9e6c93ae782e359aefe3e8fa6ea1b363c6d5c01dfa6f1fe63d
```

This is evidence that the real Redis projection can feed the already tested
legacy-compatible fact/rule boundary. It is not a production strategy result.

## Real Q2 probe

The Q2 probe read 5,224 symbols with row coverage `1.0`, but all 5,224 were
stale at the post-close observation time. The result therefore remains:

```text
status                         STALE
consistency                   BEST_EFFORT_STALE
same_observation_deterministic true
```

Artifact:

```text
/home/exedev/validation/engine-core-ed0ff38df12c5fb0a004b17a216d9b340c9dc443/live-20260918/q2-probe.json
sha256=815c9d8c773449f2e9f6f328a045dcbe058c9c96cfd12ff2efafa43fe1a0fd88
```

`coverage=1.0` is not promoted to `READY`; freshness/completeness remains a
separate quality dimension. The post-close Q2 result is not used to claim an
open-session runtime readiness or historical knowledge cutoff.
