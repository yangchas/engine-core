# TASK-008: standalone auction-anchor fact validation

Date: 2026-09-29 (Asia/Shanghai)

## Scope and alignment

This change closes one narrow Core migration gap: emit each symbol's captured
0920/0924/0925 auction anchor as a standalone fact, independently of adjacent
anchor deltas. A missing 0920 or 0924 value must not suppress a valid 0925
anchor, and the latest quote price must never be substituted for a missing
auction anchor.

This remains a Core shadow/replay path. It does not change t1-v2, production
Redis/TD/Rabbit, ACK, services, or effects. It does not change M3-1's gates.

## Contract changes

- Added `AuctionAnchorFactV1` with per-symbol status, nullable anchor price,
  source record time/layer, evaluation/freeze times, explicit unknown
  historical-availability status, and separate content/evidence hashes.
- Q2 normalization now carries only the three anchor fields' quality metadata
  (`PRESENT_VALUE`, `MISSING`, `INVALID`) into the canonical projection; raw
  Redis fields remain excluded. The canonical Q2 projection is version 2 so
  this mapping change is reflected in projection hashes/schema identity.
- Auction strategy emits the standalone fact before adjacent-delta readiness
  checks. Existing adjacent facts and their UNKNOWN behavior remain unchanged.
- The Task-008 runner reports standalone anchors separately from
  `facts_by_symbol` (which continues to mean adjacent facts).

## Real frozen-source replay

Input: `/home/exedev/validation/task008-3s-real-replay-20260918-0915-0940-20260926T224518+0800/q2frame.jsonl`

Pinned input SHA-256:

```text
08187d216274180e407565463f4ea482442748f7b70e57d5568418bd35518262
```

The source is the frozen Q2Frame produced by the exact t1-v2 release identified
in its inventory. It is not a live Rabbit delivery capture.

The two-pass real replay artifact is:

`/home/exedev/validation/task008-q2frame-anchor-fact-v1-quality-20260929T081125+0800/q2frame_auction_core.json`

Artifact SHA-256:

```text
ad33d578407bd35a47b3dde454b92a079683069f99a4c99ce57e6c83fda5d341
```

It reports ordered/repeat determinism and, through the first-observable 09:25
barrier:

- 202 frames and 225,829 updates, with one Engine;
- 5,221 Q2Frame symbols;
- standalone 0925 anchor facts: 5,171 `AVAILABLE`, 50 `MISSING`;
- adjacent 0925 price deltas: 3,472 available, 1,749 `UNKNOWN`;
- historical `available_at` remains `UNKNOWN` for every anchor.

After the Q2 projection schema was bumped to V2, the final code was run once on
the same frozen source. The resulting standalone 0925 fact map matched the
prior ordered/repeat artifact exactly for all 5,221 symbols. This final-code
single pass processed 202 frames / 225,829 updates, used one Engine, reached
09:25:06, and retained the same anchor and adjacent-delta counts. This is a
fact-map parity check against the prior two-pass real run; it is not a new
two-pass determinism claim for the V2 projection schema.

No synthetic market data was used for the real replay. Unit tests separately
cover missing-prior-anchor behavior and ensure an available 0925 anchor remains
independent while its adjacent delta stays UNKNOWN.

## Verification

```text
pytest: 727 passed, 3 dependency deprecation warnings
compileall: PASS
git diff --check: PASS
```

The three warnings originate from the installed protobuf runtime in the
existing canonical-tick fixture test; no test failed.

## Limits and status

- `source_record_time_ms` is the per-symbol Q2 `ts` value. It is not proof of
  Rabbit arrival time or historical `available_at`.
- The replay validates frozen t1-v2 Q2Frame facts, not live 09:25 queue timing,
  live Redis snapshot behavior, or full historical arrival order.
- This change emits missing-anchor facts; it does not perform Wencai recovery
  or write Redis.
- No live Redis/TD/Rabbit access or production side effect occurred.
- `TASK-008=PARTIAL_EVIDENCE`; this narrow closure does not claim the whole
  opening/replay task complete or authorize the next phase.
- `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` remain unchanged.
