# Auction Engine report projection shadow — 2026-09-18 16:29 CST

## Scope

Commit `a78e5a9` extends the existing real TD auction Engine shadow with the
build-only `AuctionFactReportArtifact`. The runner still reads only bounded
`auction_snapshot_v2` rows and keeps `engine-next` as the report/effect owner.
No Redis/TD write, Rabbit consumer/ACK, claim, SMTP, notification, or order
path is assembled.

## Real result

```text
trade_date                 2026-09-18
symbol                     600519
processed_signals          6
engine_fact_status         PARTIAL
engine_fact_only           true
direct/engine semantic     equal
report status              PARTIAL
report fact status         PARTIAL
report side_effect_free   true
```

The projected text contains only objective fact fields:

```text
amount_delta_yuan: 6946387
pressure_delta_yuan: 378898
price_delta_milli: -10
order_book: PRESSURE_IMPROVING
amount: VOLUME_EXPANDING
price: PRICE_UNKNOWN
```

It contains no `BUY`/`PASS`/strategy conclusion. The report semantic and
evidence identities are kept separate:

```text
report semantic hash = 199e9d72c0355fa873ad96abdf36d7c62c0eeee973082a9637667308cf54b617
report evidence hash = 5200f062512c2d476ec446e8faf599c66d1baa1f484ac57657c2e3151f784e5e
```

Artifact copied without editing:

```text
tmp/real-reference-20260918/auction-engine-report-20260918-1629.json
SHA256=8d9bed2f4860ff211540031e43619ba0be6bb4d4979fc8fadef9988e0c10df7f
```

## Gate conclusion

```text
REAL_TD_AUCTION_READ_PATH       PASS
WHEEL_ENGINE_PARITY             PASS
BUILD_ONLY_REPORT_PROJECTION    PASS
REPORT_EFFECT_BOUNDARY          PASS (not connected)
LEGACY_REPORT_FIELD_PARITY      NOT YET CLOSED
CORE_REPLACEMENT                NOT READY
```
