# M2 Redis auction projection adapter — 2026-09-18

## Scope

This slice extracts only the verified read path used by the deployed
`engine-next@20260903_e272842` auction loader:

```text
HGETALL market:auction:{YYYYMMDD}:{0920|0924|0925}
→ parse meta / summary / top_amount
→ canonical Core projection
```

It does not import the legacy runtime, read the anchor fallback, query TD,
call network providers, repair or backfill Redis, consume RabbitMQ, or emit
an effect.  The adapter represents `top_amount` honestly as `TOP_AMOUNT`, not
as a full-market snapshot.

## Code and verification

- Core adapter: `src/engine_core/auction_projection.py`
- Probe: `examples/run_real_redis_auction_projection.py`
- Tests: `tests/test_auction_projection.py`
- Local suite: `517 passed`, compileall PASS, diff-check PASS.
- Cobra-ion isolated suite: `517 passed`, compileall PASS.
- Changed-file SHA-256 matched local/Cobra-ion:
  - `auction_projection.py`: `5f4b20f008d459ec0f97264a180bc227d7c18aca5dc30c3aa934af51bd4dfc92`
  - `test_auction_projection.py`: `0a1a7f59cca05bcb9995434abed5dac83373eee1701a590dd890c7d2cb104364`
  - `run_real_redis_auction_projection.py`: `2266b23e54c8f6bf61babd81347407b5aa2edde778b2693b645bca7811fd51b0`

## Real Redis result

The probe ran against Cobra-ion Redis at `2026-09-18` using only HGETALL:

- `0920`: `READY`, 200 TopN rows
- `0924`: `READY`, 200 TopN rows
- `0925`: `READY`, 200 TopN rows
- requested symbols `600519,000001,000002` were absent from all three TopN
  projections; they were not synthesized and remain explicitly missing.
- probe artifact: `/home/exedev/validation/m2-redis-auction-projection-20260918-v3.json`
- artifact SHA-256: `819962edaef581161687091f2df416822b4182dade613dc8d6673952a0e79676`
- `read_only=true`; no Redis writes were observed.

## Legacy differential

The exact legacy loader and the Core adapter were run against the same live
Redis data under a write-blocking proxy:

- legacy rows: 600 (200 per tag)
- Core rows: 600 (200 per tag)
- `(tag, symbol)` key sets: equal
- `auction_amount_yuan` and `bid_amount_yuan`: equal for all comparable rows
- legacy `ask_amount_yuan=0.0` while the source field is absent; Core keeps
  `ask_amount_yuan=None` and `ask_amount_present=false` (`Missing != Zero`).
- 56 rows have legacy `price/change_pct=0.0` where the source projection has
  no price/change; Core keeps both fields missing. This is an intentional
  semantic correction, not a numeric parity failure.
- Redis write calls observed through the legacy proxy: none.

## Boundary

This closes `core_consumes_redis_auction_projection` for the bounded
TopN-projection path only. It does **not** close full-universe auction
coverage, 0920→0924 adjacency, AuctionState/freeze ownership, or
engine-next replacement. Production `engine-next` and `t1-v2-live` remain the
owners.
