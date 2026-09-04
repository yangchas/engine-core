# Q2Frame replay audit

Date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/feature-engine-integration`

## Implemented scope

```text
Q2FrameV1 mapping
  -> Q2FrameReplaySource
  -> VirtualClock.advance_to(logical_ts_ms)
  -> normalized Q2ProjectionSnapshot
  -> existing EngineSignal(MARKET_UPDATE)
  -> existing DeterministicEngine
```

The adapter is in-memory and read-only. It validates the existing Q2FrameV1
version, positive sequence, continuous `seq_no`, non-decreasing
`logical_ts_ms`, list-shaped updates and six-digit symbols. It does not infer
Rabbit arrival order, batch timing, or TD event-time semantics.

## Exact equivalence result

The same three-frame 600519 Q2 fixture was run through:

1. `Q2FrameReplaySource + VirtualClock + DeterministicEngine`.
2. Direct canonical Q2 projection fixtures + the same Engine.

Snapshot and Probe result hashes matched for every Engine trigger. The replay
source also produced identical projection hashes on two independent runs.

## Verification

```text
local: 71 passed
local compileall: passed
Q2Frame version/sequence/time guards: PASS
VirtualClock monotonic advancement: PASS
same Engine queue composition: PASS
Q2Frame vs canonical fixture: EXACT_EQUIVALENCE
```

TD event-time replay, Rabbit arrival/batch equivalence, checkpoint/restart and
production writes remain explicitly deferred.
