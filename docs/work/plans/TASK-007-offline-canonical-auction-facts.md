# TASK-007 — Offline canonical replay / auction facts

## Scope

Build the smallest pure-Core seam from a Rabbit-primary `TickBatchV1` to the
existing 3-second cross-sectional replay and from already-observed auction rows
to the existing versioned `AuctionTimeline`. The seam is an offline adapter and
does not own Rabbit, TDengine, Redis, Wencai, scheduling, persistence, or
effects.

## Contract

- `MarketTickV1` remains the only canonical tick authority.
- A batch is accepted only when every tick has the batch trade date and the
  batch is in `REPLAY` mode (or an explicit offline mode).
- Missing/unknown canonical fields are preserved as missing facts; the adapter
  never substitutes zero or fabricates a `TDEventV1` value.
- Valid ticks are projected to the existing legacy replay event shape and then
  fed through `CrossSectionReplaySource` one frame at a time.
- Every configured frame, including empty frames, remains in the timeline.
- Auction observations are passed to `AuctionTimeline`; 0920/0924 are optional
  for 0925 analysis, and late/repeated cohorts remain revisioned/idempotent.
- Outputs are `FACT_ONLY`/offline evidence. No strategy conclusion or effect is
  emitted.

## Acceptance gates

- Rabbit-primary batch conversion is deterministic and does not import client
  libraries.
- Missing required tick fields produce an explicit `PARTIAL`/`BLOCKED` result,
  never a fabricated zero.
- Ordered and shuffled equivalent batches produce the same frame/signal hashes.
- Empty frames and 0925-without-prior-anchor analysis remain explicit.
- Identical auction recovery content does not create a new revision; late
  content creates a new revision without overwriting old evidence.
- Existing full pytest, compileall and diff-check pass.
- `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged.

## Out of scope

Live Rabbit consumption/ACK, TD/Redis access, Wencai calls or writes, producer
changes, strategy migration, scheduler ownership, and production replay.
