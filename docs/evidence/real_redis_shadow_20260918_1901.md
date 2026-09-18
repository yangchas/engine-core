# Real Redis → engine_core Shadow Evidence — 2026-09-18

## Scope

This is a post-market, read-only validation of the existing Redis auction projection
adapter and the in-memory Core engine. It is not an opening-time acceptance and does
not replace the next normal-trading-day `09:20` run.

Safety boundary:

- `engine-next` and `t1-v2-live` remained the production owners.
- No Rabbit consumer, ACK, or publish was added.
- No Redis/TD write, repair, fallback, notification, or effect was performed.

## Runtime

- Host: `cobra-ion`
- Observation time: `2026-09-18 19:01–19:03 Asia/Shanghai`
- Code archive: `/home/exedev/validation/engine-core-6b4f726-v1`
- Python: `3.12.3`
- Redis client: `redis 8.1.0` from `/home/exedev/services/engine-next/shared/venv`

The standalone validation directory's Python 3.12 environment does not contain
`redis`; no package was installed. The existing engine-next virtualenv was used only
as the already-verified read-only runtime dependency environment.

## Probe result

`run_real_redis_auction_projection.py` read only `HGETALL` from:

```text
market:auction:20260918:0920
market:auction:20260918:0924
market:auction:20260918:0925
```

The `0925` projection was `READY` for its `TOP_AMOUNT` scope, but the requested
symbols `000001`, `000002`, and `600519` were absent from the 200-row projection.
This is an expected scope limitation, not a missing-data-to-zero conversion.

## Core shadow

### `600519`

- Artifact: `/home/exedev/validation/engine-core-shadow-20260918-1901-600519.json`
- SHA256: `8d7eee3620a01256efa72c6c19db83da3f8a53927c6ffef19efc8c9150c43df8`
- Result: `MISSING`, `FACT_ONLY`
- Reason: symbol is not present in the Redis `TOP_AMOUNT` projection scope.

### `000338`

- Artifact: `/home/exedev/validation/engine-core-shadow-20260918-1902-000338.json`
- SHA256: `ed5164a0dd514b6a3caf0ccad4e519d13f0cadb890b34a2a02b5af12c0e51ebf`
- Repeat artifact: `/home/exedev/validation/engine-core-shadow-20260918-1903-000338.json`
- Repeat SHA256: `8954c4c93df3a2acd1a5da2996b0e731649be7dd246c1feaa3257b0d2de1fb25`
- Result: `PARTIAL`, `FACT_ONLY`
- `amount_delta_yuan`: `24231638`
- `0920/0924/0925` projection statuses: `PARTIAL/PARTIAL/PARTIAL`
- Missing price and order-book fields remained unknown; no zero-fill occurred.
- Semantic fact hash: `bd7660727cbe9967248b99c2ff1ad4bc502d0c5c9a044e55d1efd14379d1582a`
- Projection hashes were identical across repeated runs:
  - `0920`: `cb793227ebb88d1f6aa6d7b7f83201471a811dde5f77450184fcccdf05788abe`
  - `0924`: `8d3208a5c1bdd2d3628e88914eb6d9f9d762e293cbfbec7479c41cd337f9ce55`
  - `0925`: `12ca3803c30ebb106c7474b2d7a4f3710145bf8847a4aa6d36dc1943aad4d054`

The evidence hash changed between runs because the observation/evidence context was
different. That is expected and does not invalidate semantic repeatability.

## Conclusion

The current Core Redis seam is executable against real production data in a read-only
post-market shadow. It is not yet a production replacement path. The next required
acceptance remains a normal trading-day pre-`09:20` run with a truthful Q2/reference
prefetch, followed by the existing M3-1 gate. The Redis `TOP_AMOUNT` scope must not be
treated as a full-market per-symbol source.

