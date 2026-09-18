# M2 Redis auction projection adapter — 2026-09-18

## Redis projection through the public Core Engine queue

The bounded adapter is now consumed by a separate read-only Engine shadow
runner (`examples/run_real_redis_auction_engine_shadow.py`). The runner
submits one `MARKET_UPDATE` and one same-time `TIMER` for each of `0920`,
`0924` and `0925` to the existing `DeterministicEngine`; it does not add a
provider layer, alter production owners, or write Redis/TD.

- Local full suite after this seam: `521 passed`, compileall PASS.
- Cobra-ion Python 3.12.3 isolated full suite: `521 passed`, compileall PASS.
- Real Cobra-ion run for common TopN symbol `000338`:
  - `processed_signals=6`, `strategy_result_count=3`
  - all three Redis projections were `PARTIAL` at the canonical Q2/Engine
    boundary because the projection does not provide a verified milli-price
    contract or full-universe state;
  - final fact was `PARTIAL/FACT_ONLY` with `amount_delta_yuan=24231638`,
    `rest_bid_delta_yuan=89760`, and price/ask/pressure left unavailable
    where the source contract does not support them;
  - Redis source times were preserved: `0920=1789694403287`,
    `0924=1789694650292`, `0925=1789694706197`.
- Real artifact (latest code): `/home/exedev/validation/m2-redis-engine-shadow-20260918-000338-v2.json`
- Artifact SHA-256: `686dff98811a9f0f3e207e31883fad2f31765201323c9fb646f73dfeb3efce28`
- Production `engine-next` and `t1-v2-live` remained `active`; no Redis/TD
  write, Rabbit action, restart, notification or effect occurred.

This closes only the bounded `Redis projection -> existing Core Engine queue`
consumption seam. It does not close full-universe coverage, price authority,
AuctionState/freeze ownership, or `engine-next` replacement.

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

## Core read seam differential

The existing Core Redis↔TD projection comparison tool now calls the Core
adapter for its snapshot reads; it no longer re-parses `HGETALL` JSON itself.
The anchor remains a separate read because it is a different source scope.

- Local/Cobra suite after seam integration: `518 passed`, compileall PASS.
- Real bounded symbols remain `NOT_COMPARABLE` when absent from TopN; this is
  an explicit projection-window result, not a zero-filled mismatch.
- Real common TopN symbol `000338` comparison artifact:
  `/home/exedev/validation/m2-redis-td-adapter-compare-20260918-000338.json`
- Artifact SHA-256: `d12ce2832af5cbef53a0410379d1f6a555cd05b1de162336917df9ff0a3d50a5`
- 0920/0924/0925: `match_amt_yuan=MATCH`, `rest_bid_amt_yuan=MATCH`,
  `rest_ask_amt_yuan=NOT_COMPARABLE` because Redis projection omits ask.
- `read_only=true`; Redis/TD writes, recovery, fallback and effects were not
  invoked.

## Boundary

This closes `core_consumes_redis_auction_projection` for the bounded
TopN-projection path and its existing read-only comparison seam. It does **not** close full-universe auction
coverage, 0920→0924 adjacency, AuctionState/freeze ownership, or
engine-next replacement. Production `engine-next` and `t1-v2-live` remain the
owners.
