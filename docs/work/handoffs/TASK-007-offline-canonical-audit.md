# TASK-007 handoff — offline canonical replay / auction facts

Implementation branch: `codex/task-offline-canonical-auction-facts`  
Implementation commit: `43a3dfc99c191fc2a3998879e7de26da51b1a2f5`  
Status: `REVIEW`

## Delivered

- `OfflineCanonicalReplay` admits only `REPLAY`/`OFFLINE`/`HISTORICAL`
  `TickBatchV1` modes.
- Rabbit-primary canonical ticks project to the existing `TDEventV1` replay
  oracle only when required values are `PRESENT_VALUE`.
- Missing, unknown, invalid, and proto3-ambiguous required values remain
  explicit `PARTIAL`/`BLOCKED`; no zero fill is introduced.
- Batches stream into one global 3-second frame at a time and retain empty
  frames; one `MARKET_UPDATE` is submitted per frame when replaying an Engine.
- Existing `AuctionTimeline` remains the owner of optional 0920/0924 facts,
  0925 recovery planning, idempotent revisions, and late correction evidence.
- Contract documentation: `docs/CANONICAL_REPLAY_AUCTION_FACTS_V1.md`.

## Verification

- Server Python 3.12.3: `667 passed`, 3 existing protobuf deprecation warnings.
- `compileall -q src tests examples`: PASS.
- `git diff --check`: PASS.
- Canonical replay module/tests contain no Rabbit, Redis, TDengine, Wencai,
  network client, writer, ACK, scheduler, or effect import.
- No Redis/TD/Rabbit access, production service restart, or production source
  modification was performed.

## Limits

- This is an offline pure-Core seam, not Rabbit live ingestion and not a
  production replacement.
- Historical arrival order and `available_at` remain UNKNOWN.
- `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

## Review gate

Do not fast-forward to `codex/feature-session-engine-integration` until an
integrator verifies the above evidence and the full suite again.
