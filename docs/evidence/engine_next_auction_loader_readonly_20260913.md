# engine_next auction snapshot loader read-only probe (2026-09-13)

## Scope

This probe exercised only the exact legacy
`IntradayDataHub.load_auction_snapshots()` method behind the existing
fail-closed Redis guard. It did **not** construct `IntradayContextBuilder`,
call recovery/fallback paths, access TDengine or network connectors, consume
RabbitMQ, or write Redis/TDengine.

The probe source is `examples/run_engine_next_auction_loader_probe.py` and was
run from commit `aa10253` (`codex/feature-session-engine-integration`).

## Cobra-ion verification

| Item | Result |
| --- | --- |
| Archive SHA-256 | `5bb4bc02c7e36f6a30d007769968bb6622529555fde269965a7c6b791207b634` |
| Runtime | `/home/exedev/services/engine-next/shared/venv/bin/python` |
| Python | 3.12.3 |
| Isolated suite | 279 passed |
| `compileall` | PASS |
| Real Redis probe | `read_only=true`, no blocked writes |

The real Redis probe was run for the bounded symbols `000001`, `000002`, and
`600519` with tags `0920`, `0924`, and `0925` for both `2026-09-11` and
`2026-09-13`. The loader returned zero rows for both dates. This is an
observation about the current Redis retention/content, not a synthetic success:
the keys for `2026-09-13` exist but their `top_amount` payloads are empty, while
the `2026-09-11` snapshot keys had expired by probe time.

## Findings

1. The narrow legacy snapshot reader is side-effect-free under the guard: no
   Redis mutation was attempted.
2. The old context builder must not be used as the core read path: a separate
   probe previously observed blocked `delete`/`hset` attempts in that path.
3. The legacy result field `redis_keys_written` is misleading for this method;
   it carries keys read. The new probe reports this as
   `legacy_keys_reported` and does not treat it as a write claim.
4. Historical Redis `0920/0924/0925` rows are not a reliable source of a
   non-empty historical pair. A future real trading-day capture or TD
   projection remains necessary for the first non-empty migration fixture.

## Status

`LEGACY_AUCTION_SNAPSHOT_READ_BOUNDARY = OBSERVED`.

This evidence authorizes a later thin Provider extraction of this read method,
but does not claim that Redis snapshots are retained, complete, or semantically
equivalent to the TD auction projection.
