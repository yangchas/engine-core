# Real reference-source probe (2026-09-10 data, current core commit)

## Scope

On 2026-09-13, the current `engine_core` commit was used on `cobra-ion` to
invoke the existing `engine_next` connector paths in a bounded read-only
probe. The probe requested one symbol (`600519` where supported), at most
three rows per source, and wrote only an isolated validation artifact. It did
not run a strategy, write Redis/TDengine, consume RabbitMQ, repair caches, or
send notifications.

## Identity

| Item | Value |
| --- | --- |
| Core commit | `648b07e` (`648b07e...`) |
| Legacy release | `e272842c8f490f55a1b017badb71e71904ce008e` |
| Legacy root | `/home/exedev/services/engine-next/releases/20260903_e272842` |
| Runtime | `/home/exedev/services/engine-next/shared/venv/bin/python` |
| Python | 3.12 (production shared venv) |
| Probe artifact | `/tmp/core-shadow-648b07e/reference-probe-20260910.json` |
| Artifact SHA-256 | `7DBD403888ADC9BEAA07E6E18B54A15661D0EC61E43BB4F532A5B02D84C0E591` |
| Requested trade date | `2026-09-10` |

## Results

All six configured connectors reported `connection_status=PASS`. Contract
status is deliberately source-specific:

| Source | Contract | Runtime use | Evidence |
| --- | --- | --- | --- |
| BaoStock daily kline | `PASS` | candidate dated historical input | requested and returned date both `2026-09-10`, one row for 600519 |
| Kaipan hot plates | `OBSERVED` | not admitted to replay runtime | response contains rows, but response is not self-dated |
| Kaipan yesterday limit pool | `OBSERVED` | not admitted to replay runtime | response contains rows, but historical availability is not proven |
| Kaipan ban reasons | `OBSERVED` | not admitted to replay runtime | current query has no verified requested trade date |
| THS hot rank | `OBSERVED` / `UNAVAILABLE` historical role | oracle/observation only | no dated historical runtime contract |
| Wencai limit truth | `OBSERVED` / `UNAVAILABLE_WITHOUT_DATED_QUERY_EVIDENCE` | oracle/observation only | no proof that the result was knowable at replay cutoff |

## Interpretation

This is a real connector and response-shape check, not a claim that all
sources are production-ready. Only the BaoStock row currently satisfies the
probe's explicit request/response-date contract. For every other source,
`TemporalDataGuard` must keep runtime/replay use unavailable until a dated
availability contract is supplied. A successful query today is not evidence
that the historical result was knowable at the requested intraday cutoff.

The probe reuses the legacy connector access path; it does not create a new
Redis/TD/network access hierarchy. Business interpretation remains in
`DataFunction`, and no strategy or engine code calls a connector directly.

## Status

```text
REAL_CONNECTIVITY = PASS (6/6)
DATED_DAILY_KLINE = PASS (BaoStock only)
OTHER_REFERENCE_RUNTIME_CONTRACTS = OBSERVED / BLOCKED
SIDE_EFFECTS = 0
```
