# TASK-008 speed unit mapping — 2026-09-29

## Result

The speed_1m unit mapping is now implemented and verified against the frozen
real t1-v2 Q2Frame replay:

    Q2 spd1m (basis points) / 10000.0 = opening speed_1m (decimal ratio)

This closes the field-specific UNKNOWN_UNIT_MAPPING noted in the earlier
Q2Frame-to-Core opening handoff. It does not close TASK-008 as a whole.

## Source basis

- stock-situation-runtime/C/t1_v2/quote_calculator.cpp::calc_speed_bp emits
  an integer basis-point change from the current and one-minute reference
  prices.
- stock-situation-runtime/C/t1_v2/redis_v2_writer.cpp publishes that value
  under spd1m.
- Existing stock-situation-runtime/engine_next/runtime/intraday_data_hub.py
  maps spd1m to speed_1m by dividing by 10000.0. The earlier same-input
  differential is recorded in
  docs/evidence/engine_next_q2_same_input_differential_20260917.md (3/3
  observed).
- Core's Q2_FIELD_CONTRACT already identifies speed_1m_bp as an integer
  basis-point field. The Core opening adapter now applies the same conversion,
  preserves missing/invalid field status independently, and does not let this
  optional field degrade the primary opening fact.

## Real-data replay evidence

Input is the frozen 2026-09-18 Q2Frame artifact produced by the existing
t1-v2 replay path, not generated test rows:

    path: /home/exedev/validation/task008-3s-real-replay-20260918-0915-0940-20260926T224518+0800/q2frame.jsonl
    SHA-256: 08187d216274180e407565463f4ea482442748f7b70e57d5568418bd35518262
    inventory: 500 frames, 5,221 symbols, 1,227,873 updates, 97 empty frames

Core ran the opening shadow through 09:32:10 using one Engine. At that cutoff
it consumed 343 frames / 450,388 updates. The runner repeated the same pinned
input and reported deterministic equality for opening evidence, processed
signals, reducer revision, final state hash, virtual clock, and input hash.

An independent read-only pass over those first 343 frames retained the latest
spd1m for each symbol and compared int(spd1m) / 10000.0 with Core's
OpeningFactV1.speed_1m:

    symbols compared: 5,221 / 5,221
    missing source speed: 0
    invalid source speed: 0
    mismatches: 0
    maximum absolute difference: 0.0
    speed_1m field status: AVAILABLE=5,221
    observed speed values: zero=1,604; nonzero=3,617; range=[-142, 236] bp

Replay output:
/home/exedev/validation/task008-speed-map-20260929T183512+0800/task008-q2frame-opening-speed-map.json

Output SHA-256:
cf959ac9403e21c5decb378b57e334da1c8055b8db9018b84e3f6834892c4720

Independent comparison summary:
/home/exedev/validation/task008-speed-map-20260929T183512+0800/speed_field_parity.json

## Verification and limits

    pytest: 730 passed (3 upstream protobuf deprecation warnings)
    compileall: PASS
    git diff --check: PASS
    production reads/writes/restarts/effects in this run: NONE

The replay did not connect to Redis, TDengine, or RabbitMQ. It verifies the
Core mapping against a real, SHA-pinned historical Q2Frame artifact and the
existing source conversion contract; it does not establish Rabbit arrival
order, historical available_at, live snapshot atomicity, or full-market
membership. The overall opening cohort remains PARTIAL (5,209 READY, 12
PARTIAL); 09:20/09:24 remain PENDING and 09:25 remains PARTIAL in this
replay. TASK-008=PARTIAL_EVIDENCE, NORMAL_OPENING_ACCEPTANCE=UNPROVEN.

AVAILABLE here means the published integer field was present and its unit
conversion was applied, matching the existing engine-next consumer. It does
not prove that a zero has a valid one-minute reference: t1-v2's calc_speed_bp
also returns zero when that reference slot is unavailable, and Q2 does not
carry a separate reference-validity flag. The replay observed 1,604 zeros;
those are preserved as the producer/consumer currently represents them, not
reclassified as missing or as confirmed flat price movement.

The opening shadow remains FACT_ONLY; this change only makes the independently
unit-aligned speed_1m fact available. It adds no strategy threshold or effect.
