# Real reference-source probe — 2026-09-13

## Identity

- Core archive tested: `e21bf00caf8a2a278dc7b233c04e0be3d8ed0250`
- Legacy runtime: `engine-next` release `e272842c8f490f55a1b017badb71e71904ce008e`
- Server: `cobra-ion`
- Python: `3.12.3` (`/home/exedev/services/engine-next/shared/venv/bin/python`)
- Audit trade date: `2026-09-10`
- Symbol probe: `600519`; maximum rows per source: `3`
- Evidence artifact SHA-256: `440f80ccbd77189d830f577ca611e5e054071ad01f69d88dd29ec521dbf0c9ad`

## Observed result

The bounded probe used the existing `engine_next` connector instances and made
one read request per source. It did not assemble Redis/TD writers, repair,
notifications, SMTP, ACK handling, or strategy effects.

| Source / dataset | Connection | Contract/date result | Notes |
|---|---:|---|---|
| Baostock daily kline | PASS | PASS | Requested and returned date both `2026-09-10`; one `600519` row. |
| Kaipan hot plates | PASS | OBSERVED | Rows returned, but the response does not carry a self-verifying historical date. |
| Kaipan yesterday limit pool | PASS | OBSERVED | Rows returned; response date is not independently self-dated. |
| Kaipan ban reasons | PASS | OBSERVED | Row returned, but no matching `source_trade_date`. |
| THS hot rank | PASS | OBSERVED | Current query has no date parameter; not a historical runtime input. |
| Wencai limit truth | PASS | OBSERVED | Current query has no structured date; not a historical runtime input. |

## Interpretation

This is real connector/network evidence, not a unit test. It proves that the
six legacy connector paths were reachable from the Cobra runtime and records
their actual response shape. It does **not** upgrade OBSERVED sources to
historical runtime authority. In particular, a result observed today does not
prove that it was knowable at an earlier replay cutoff; `TemporalDataGuard`
must continue to reject unknown `available_at_ms` for historical runtime use.

The probe output displayed mojibake through the PTY for Chinese text, but the
JSON artifact was written as UTF-8 by the probe. This is a terminal rendering
limitation, not evidence that the connector payload was re-encoded by
`engine_core`.

## Gate status

- `REAL_CONNECTIVITY_PROBE`: PASS (6/6 connection paths)
- `BAOSTOCK_DATED_DAILY_KLINE`: PASS for this request
- `KAIPAN/THS/WENCAI_HISTORICAL_AVAILABILITY`: OBSERVED / not runtime-ready
- `PRODUCTION_CHAIN_ACCEPTANCE`: NOT evaluated by this probe
- `ENGINE_CORE_PRODUCTION_REPLACEMENT`: NOT claimed
