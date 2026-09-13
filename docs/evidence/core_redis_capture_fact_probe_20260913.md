# Core fact probe from captured Redis auction projections — 2026-09-13

## Scope

This is a local, deterministic fact-wheel run over two **real production
capture files** from `cobra-ion` (`2026-09-07`, tags `0920` and `0925`).  The
files were produced by the existing read-only `ground_truth_capture.py`, not
hand-authored fixtures.  No live Redis/TD connection, writer, Rabbit consumer,
or strategy was used in this step.

Source capture SHA-256:

```text
auction_0920.json  9a344e0e065b07e7349480a0ea161ea157b9cf0b51d94a0594bd3f3b2a7c7a82
auction_0925.json  a3b653ac1a0ab11e0cae3255744ca264135ac9b5e05e6fe16fdfa667794e71fd
```

The capture envelope's `top_amount` rows were normalized to the core symbol
state contract (`price_milli`, `auction_amount_yuan`,
`auction_bid_amount_yuan`, `auction_ask_amount_yuan`, `volume_lots`) and passed
to `build_segment_frame` without changing the raw source timestamps.

## Result

The three symbols present in both Top-200 projections produced stable fact
frames for the non-adjacent exploratory interval `0920 → 0925`:

| Symbol | Frame status | Coverage | Price return | Auction amount delta (yuan) | Order-book status |
|---|---|---|---:|---:|---|
| 600519 | `PARTIAL` | `PARTIAL` | -45 bp | 19,900,500 | `UNAVAILABLE` |
| 300308 | `PARTIAL` | `PARTIAL` | 164 bp | 360,720,896 | `UNAVAILABLE` |
| 688825 | `PARTIAL` | `PARTIAL` | 110 bp | 381,516,735 | `UNAVAILABLE` |

The frame hashes were:

```text
600519 content=25a0095ef2bc96f1b1d9c2d87019bd607f35a321ea326ebec4ad1f54653ef12d
300308 content=80d5499e0bb9a1e61235466b98c4dbc616d7c77fed73d44bd99b9c689abf2b33
688825 content=ca85ef2c4f70273c50cb18708fcd856d55aa36bc169e8f548a3d7fd155c51872
```

## Important boundaries

- This is **not** the required adjacent `0920 → 0924` comparison.  No 0924
  capture exists in this evidence, so `compare_adjacent_segments` was not
  invoked and no adjacent comparison hash is claimed.
- The real Redis projection has no ask amount in these rows.  The core kept
  `auction_ask_amount_yuan=None`, therefore resting pressure, pressure delta,
  and the order-book fact remain `UNAVAILABLE`; no zero fill was introduced.
- The projection has no `volume_lots` field.  Amount is available from the
  explicit auction amount field, but volume remains `UNAVAILABLE`.
- `coverage=PARTIAL` is intentional: these are Top-200 projections, not a
  full-universe immutable snapshot.  The business anchors (`09:20:00` and
  `09:25:00`) remain separate from source record times (`09:20:00.3022` and
  `09:25:06.010`).
- The output is fact-only.  It does not infer `TURN_STRONG`, buy/avoid, net
  capital flow, or any production strategy decision.

## Conclusion

```text
real captured Redis projection can enter core fact wheels       PASS
price/auction amount normalization                             PASS
missing ask/volume preserved without fake values               PASS
adjacent 0920→0924 evidence                                    UNAVAILABLE
production replacement readiness                               NOT CLAIMED
```

The next trading-day capture must add a real 0924 projection before any
adjacent segment or legacy-rule parity claim is made.

