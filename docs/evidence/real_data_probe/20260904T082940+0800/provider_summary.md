# Real Data Probe summary

The probe ran read-only on `cobra-ion` using the existing server virtualenv
clients. No production queue was consumed, no Redis/TD writes were issued, and
no credentials or cookies were copied into this evidence.

## Redis Q2

- Redis ping succeeded.
- Available cohorts at probe time: `20260902`, `20260903`.
- Latest cohort: `q2:active:20260903`, 5,217 symbols; total `q2:*` key count 5,219.
- Sample `000001` contains the producer field names recorded in the manifest.
- Q2 is a best-effort projection cohort; no global generation was observed.

## TDengine

- Native client connected to `market_data1`.
- `stock_tick_v2` schema matches the existing C++ query builder (milli prices,
  integer yuan/units, five-level prices/volumes).
- Read-only count for 2026-09-03 09:20-09:24 was 82,183 rows.
- `daily_kline` is readable, but sampled `volume=0` must not be assumed to mean
  a verified zero until the writer/consumer semantics are traced.

## Use boundary

This probe is evidence and fixture-capture input only. It does not establish
historical `available_at` semantics and cannot make a current query eligible as
a replay runtime input. All future providers must pass `TemporalDataGuard`.
