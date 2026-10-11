# Task009-F follow-up: batch semantics and classification of field differences (2026-10-09)

Inputs: `docs/evidence/task009-t1-prefix-q2frame-vs-capture-20261011.md`, validation directory
`/home/exedev/validation/task009-f-t1-prefix-q2frame-vs-capture-20261011/`. Full-field parity is **not**
claimed: only 12 of 5,227 capture symbols have every capture field equal.

## What the replay batches are (and are not)
Source facts (t1-v2 release 20260923_tdstop0945b):
- The TD replay source reads one half-open 3 s window per query, orders rows by `(ts, symbol)` and emits
  **one batch per `tss` timestamp group** (`replay_timestamp_group_size`); a 3 s window therefore becomes up to
  three batches with per-second logical time. Observed: 1,190 Q2Frames, one per distinct whole second
  (maximum frames sharing a second: 1), none empty; 79 clock events are not Q2Frames.
- Observed gaps between frames: 09:25:02 -> 09:26:00 (58 s) and 09:26:57 -> 09:30:00 (183 s): the TD table
  holds no rows there, so the replay has nothing to emit; the clock events advance time only.
- A Rabbit `DataBatch` carries `batch_id`, `records`, `sent_at`; the decoder drops `batch_id/sent_at` and
  builds a `TickBatch` per delivery, deriving `logical_ts_ms` from the max tick ts when none is supplied.
  Delivery boundaries, membership, order and arrival time are not recoverable from TD rows or from the
  live progress counters.

Proved: replay batches are timestamp groups produced by the TD reader. Not proved (UNKNOWN): any mapping
between these batches and live Rabbit deliveries, live arrival order, completion watermark, or whether the
live stream held more records per `(symbol, ts)` than TD. This run is **not** Rabbit batch equivalence.

## Classification of the 5,227-symbol comparison
| class | fields | status |
|---|---|---|
| matches under same source ts | a20 a24 a25 am br ar pc ph ls; px amt vol amt2m amt5m spd1m vec3m vec5m | proved by this run (one date, one instant) |
| time-alignment only | all mismatches of px/amt/vol/amt2m/amt5m/spd1m/vec3m/vec5m/ts (163 symbols, replay source ts newer by 3-21 s) | expected from capture timing/dirty-gating; cause of the 134 symbols whose amt/vol changed is UNKNOWN; not a calculation error |
| source-semantics | iv, ia, ln: latest-event state at write time | expected difference class; live multiplicity of same-ts records UNKNOWN |
| input-shape | mx, mn (live range wider than TD-row range) | consistent with live records absent from TD; UNKNOWN |
| label | mk: 618 symbols, all 688xxx, replay `kc` vs live `sh` | proved: the TD replay converter derives `kc` from the prefix when TD rows have no market/exchange; Core's equity filter accepts both |
| expected UNKNOWN | the 3 ts=00:00:00 placeholders outside the replay window | window starts 09:15:00 |
| defects | none identified in Core or in t1-v2 by this run | absence of evidence for this date only |

## Evidence hygiene
Original Q2Frame, logs and binary copy are unmodified. The comparator now hashes with streaming reads and
runs the repeat-determinism pass only when `--repeat-determinism` is given; the original run executed it
(`projection_deterministic_on_repeat=true`).
