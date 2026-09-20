# Offline canonical replay / auction facts V1

`OfflineCanonicalReplay` is the pure Core seam between the Rabbit-primary
`TickBatchV1` contract and the existing 3-second cross-sectional replay. It
accepts already-parsed batches only. It does not consume Rabbit, query TD or
Redis, call Wencai, schedule timers, persist data, or emit effects.

## Batch admission

Only `REPLAY`, `OFFLINE`, and `HISTORICAL` batch modes are admitted. A `LIVE`
batch is rejected so an offline result cannot be mislabeled as normal runtime
evidence.

Each canonical tick must have `PRESENT_VALUE` for `px_milli`, `pc_milli`, and
`amt_yuan` before it is projected to the legacy `TDEventV1` oracle. Missing,
unknown, invalid, or proto3-ambiguous required fields are never replaced with
zero. A batch with some safe ticks is `PARTIAL`; a batch with no safe tick is
`BLOCKED`; an explicitly empty batch is `EMPTY`. Skipped tick diagnostics retain
their event time so a partial/blocked result is attached to the correct frame.

## Frame stream

`iter_frame_results()` retains only the current pending frame and feeds the
existing `CrossSectionReplaySource` with one global half-open 3-second frame at
a time. It emits every configured frame, including empty frames. Input batches
must be non-decreasing in event time; deterministic ordering inside a frame is
delegated to the existing replay source.

The resulting frame and signal hashes are replay evidence only. Event-time
ordering is not Rabbit arrival ordering, and this adapter does not establish
historical `available_at`.

## Auction facts

`observe_auction()` delegates already-observed rows to the existing
`AuctionTimeline`. The timeline retains `0920`/`0924` as optional facts for a
`0925` analysis, preserves `UNKNOWN` prior deltas, produces a recovery plan for
partial/missing data, and creates a new revision only when content changes.
Repeated identical recovery content is idempotent. Results remain
`FACT_ONLY`; no strategy or trading conclusion is produced.
