# t1-v2 replay auction semantic parity — 2026-09-25

Status: `BOUNDED_REPLAY_PARITY_PASS / PHASE_P_PARTIAL`

## Scope and result

The t1-v2 replay development branch now matches the deployed release's
auction-calculation and anchor-projection semantics for the real
2026-09-23 pre-open window `[09:15:00,09:25:09)`. The replay used one TD
`SELECT` per 3-second half-open slice, processed all returned rows, and wrote
only to an isolated Redis DB15 namespace. It did not write TD, consume/ACK
Rabbit, or change a production service.

This is bounded real-data parity for this date/window, not proof of binary
identity, all-date equivalence, live Rabbit delivery membership/arrival order,
historical `available_at`, or NORMAL acceptance. This run ends before
continuous-session processing.

```text
REAL_TD_READ_3S_SLICES              PASS
Q2_PER_SYMBOL_HASH_PARITY          PASS (5222/5222)
Q2_ACTIVE_SET_PARITY               PASS (5222/5222)
A2_0920_0924_0925_PARITY            PASS
LEGACY_AUCTION_0920_0924_0925_PARITY PASS
LATEST_AND_0925_ANCHOR_PARITY       PASS
PRODUCTION_SIDE_EFFECTS             NONE_OBSERVED
PHASE_P                             PARTIAL
M3_1_NORMAL                         BLOCKED
TD_WRITE_HEALTH                     UNPROVEN
```

## Source discrepancy and correction

The pre-fix development build used `px_milli` directly for pre-09:25 auction
facts, used level-2 price for resting level-2 quantity, and classified ST
fallback limits differently from the deployed release. Its Redis auction
projection also serialized the latest quote price/change for every anchor and
ranked `top_chg` from the latest quote, rather than the requested 0920/0924/0925
anchor. This explained why Q2 and auction projection parity failed
independently.

Commit `ca5ece0` ports the release-calculation semantics into the replay
development branch:

- pre-09:25 matching price uses equal positive level-1 bid/ask; post-09:25
  prefers positive `px_milli`;
- pre-09:25 match amount is calculated from that effective price and the
  matched level-1 volume; resting amount uses level-1 price times level-2
  quantity;
- auction limit-state checks use side-specific level-1 prices; explicit
  `limit_band_bp` remains authoritative, while `is_st` alone does not alter
  the release fallback band;
- A2/legacy price, change, breadth, and `top_chg` use the requested anchor;
  an unavailable formal anchor stays unavailable rather than falling back to
  the latest quote.

Regression coverage exercises virtual auction prices, resting order-book
amounts, upper/lower limit seals, explicit ST limit metadata, anchor-specific
price/change, missing anchors, and Redis projection serialization.

## Build and real replay evidence

- t1-v2 branch: `codex/task-q2-pure-function`
- code commit: `ca5ece0` (`fix(replay): align release auction snapshot semantics`)
- full-dependency binary:
  `/home/exedev/validation/t1v2-source-and-anchor-full-fEq482/t1_v2_replay`
- binary SHA-256: `0f86e970607bf929bf31a39ab126e3c49bec965b1cd0c2240f0deeec15221fb7`
- built-in `--self-test`: PASS; `git diff --check`: PASS
- real-run evidence directory:
  `/home/exedev/validation/t1v2-anchorfix-real-ehL9zP/`
- source: TD `market_data1.stock_tick_v2`, trade date `2026-09-23`,
  `[09:15:00,09:25:09)`; TD host was explicitly set to the same loopback
  address used by the running service. No credential values were read into the
  report.
- isolated Redis: DB15, prefix `task009anchorfix20260925T052100:`; prefix had
  zero keys before the run and produced 5,233 keys afterward.
- summary: exit 0; `batches=204`, `clocks=1`, clock at 09:25:06,
  `source_in=ticks=212022`, `source_reject=0`, `ack=0`, `td_sql=0`,
  `redis_cmds=425502`, `redis_committed=211932`.
- output checksums are in `sha256sums.txt` in the run directory.

The isolated candidate was compared read-only to both DB5/`task009k:` and the
source-aligned DB15 control `task009pbarrierinc20260925T042334:`:

- Q2: all 5,222 hash keys, fields, and values equal; active symbol set count
  5,222 and digest
  `5dfd52d3f75efa6179f463422285c1e1254c7c124a086cbd51dd7f4d49802cb5` equal.
- A2: 0920, 0924, and 0925 hashes exactly equal (four fields each); latest A2
  hash exactly equal.
- Legacy auction: 0920, 0924, and 0925 hashes exactly equal (three fields
  each); latest hash exactly equal.
- 0925 anchor: byte-equal, 539,474 bytes, SHA-256
  `1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb`.
- Runtime counters/byte metrics are excluded from semantic comparison.
- The candidate prefix had zero hits in production Redis DB0.

The first connection attempt used the development default TD host name and
exited before reading rows (`batches=0`, `redis_cmds=0`). A read-only check of
the running service's non-sensitive host/database settings showed loopback and
`market_data1`; rerunning with that host succeeded. No credentials were
printed or added to evidence.

## Service and side-effect audit

After the run, `engine-next` and `t1-v2-live` were both `active`; their
`NRestarts` remained zero. Root and `/home/exedev` had about 22 GB available.
The candidate binary is validation-only: it was not installed, deployed,
pushed, or used to restart a service. Redis writes were limited to the unique
DB15 validation prefix; TD writes and Rabbit ACKs were zero.

## Remaining limits and next owner

- The candidate source tree remains a development branch and is not byte- or
  commit-identical to the deployed release snapshot; this parity result is
  restricted to the compared real date/window and listed Redis outputs.
- The 09:25:06 behavior was tested with the available real TD event-time set;
  it does not reconstruct historical Rabbit arrival/completion order or prove
  which keys were visible to the live process at wall-clock 09:25:06.
- `historical_available_at` and exact Rabbit delivery membership remain
  `UNKNOWN`.
- 09:25:09–09:40 continuous-session behavior for this updated development
  binary was not part of this run.
- No next task is auto-started. Keep Phase P scoped to a source/parity audit;
  M3-1 remains blocked by the independent TD health gate.
