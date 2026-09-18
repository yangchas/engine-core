# Real reference-source probe — 2026-09-18

## Scope and safety

The probe ran on `cobra-ion` against the deployed `engine-next` release
`e272842c8f490f55a1b017badb71e71904ce008e` using the existing connector
implementations. It made one bounded read per source and wrote only the
validation artifact; it did not import or assemble Redis/TD writers, repair,
notifications, SMTP, strategy, Rabbit consumers, or effects.

Artifact: `reference-probe-20260918.json`  
SHA-256:
`3632d20512571ee8f49abe01cc582debec1868286558711480f0c1086c226c18`.

Requested historical date: `2026-09-17`; symbol sample: `600519`; maximum
rows per bounded sample: `3`.

## Results

All six connector calls completed at the transport level (`6/6 PASS`):

| Source / getter | Rows | Date contract | Runtime conclusion |
|---|---:|---|---|
| Baostock `fetch_daily_kline` | 1 | explicit request and response date match | `PASS`; safe candidate for dated PreviousDayStats input |
| Kaipanla `fetch_hot_plates` | 3 | request date passed, response is not self-dated | `OBSERVED`; historical runtime use is not yet proven |
| Kaipanla `fetch_yesterday_bans_pool` | 3 | request date passed, response date not required by current wrapper | `OBSERVED`; consumer/date parity still required |
| Kaipanla `fetch_ban_reasons` | 1 | no dated response in sample | `OBSERVED`; cannot prove historical as-of |
| THS `fetch_hot_rank` | 3 | current query has no date parameter | `OBSERVED`; historical replay role `UNAVAILABLE` |
| 问财 `fetch_limitup_with_lb_days` | 3 | current query has no structured date | `OBSERVED`; historical replay role `UNAVAILABLE` |

The Baostock sample returned `trade_date=2026-09-17` and a dated daily bar for
`600519`. Kaipanla, THS, and 问财 returned usable transport payloads, but the
current connector contracts do not prove that the payload was available at the
requested historical cutoff. They remain oracle/online-observation sources,
not replay runtime inputs.

## Findings

1. Transport connectivity is not the same as temporal availability. Only the
   Baostock sample currently closes both the request-date and response-date
   checks.
2. Kaipanla responses contain mojibake in the current server/runtime decoding
   path. This is recorded as source evidence; it is not silently repaired or
   promoted to a Core field contract.
3. THS and 问财 must not be used by historical replay until a dated query or
   historical availability evidence is added.
4. No provider was added to `engine_core` by this probe. The next integration
   step is a thin `PreviousDayStats` provider using the already verified
   Baostock/TD access contract, not a generic connector framework.
