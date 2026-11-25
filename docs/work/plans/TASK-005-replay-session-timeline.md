# TASK-005 Replay session timeline integration

## Scope

Join the existing global 3-second cross-sectional replay with the existing
`AuctionTimeline` and timer contracts in a hash-only, in-memory ledger.

The ledger records:

- every frame, including `EMPTY` frames;
- 0920/0924/0925 auction revisions and their source/evaluation metadata;
- already-computed 0926/0932 timer firings;
- the 09:40 replay checkpoint.

It stores hashes and metadata only. It does not schedule timers, query Redis or
TDengine, consume RabbitMQ, write storage, or emit effects.

## Contract rules

- one explicit trade date and one `FrameManifestV1` are authoritative;
- frames must be recorded sequentially, so an empty frame cannot disappear;
- auction rows are delegated to `AuctionTimeline`; missing 0920/0924 remain
  `UNKNOWN` for 0925 deltas;
- late auction revisions remain versioned and never overwrite old evidence;
- timer firings are inputs, not scheduler output;
- checkpoint finalization requires every manifest frame;
- `Fact`/`Strategy`, replay/NORMAL, and historical availability semantics stay
  separate.

## Verification target

Use a small deterministic fixture first, then wire the runner in a later
bounded task. The first implementation must prove node ordering, empty-frame
retention, optional-anchor behavior, timer idempotency, and deterministic
session/content/evidence hashes.
