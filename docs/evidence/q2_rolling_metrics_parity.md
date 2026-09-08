# Q2 rolling metrics parity

## Scope

This audit covers the rolling fields already emitted by the production
`C/t1_v2` Q2 writer and consumed by the legacy opening path.  It does not
migrate any threshold or trading decision.

## Producer contract

- `spd1m`: exact one-minute price change, integer basis points.
- `amt2m`: cumulative amount delta using the oldest available reference in
  the preceding two minute slots, integer yuan.
- `amt5m`: cumulative amount delta using the oldest available reference in
  the preceding five minute slots, integer yuan.
- `vec3m`: exact three-minute price change, integer basis points.
- `vec5m`: exact five-minute price change, integer basis points.

Producer evidence:

- `C/t1_v2/quote_calculator.cpp`
- `C/t1_v2/minute_ring_updater.cpp`
- `C/t1_v2/redis_v2_writer.cpp`
- `C/t1_v2/self_test.cpp`

## Consumer evidence

`engine_next.runtime.intraday_data_hub._standardize_q2_quote` converts the
basis-point fields to ratios for legacy consumers and preserves both rolling
amount fields in yuan. `engine_next.runtime.intraday_context_builder` carries
all five values into the opening stock snapshot.

## New boundary

The new Q2 adapter preserves the five producer values as optional integers.
Missing remains `None`; explicit zero remains zero.  No strength label,
fallback value, or strategy threshold is introduced.
