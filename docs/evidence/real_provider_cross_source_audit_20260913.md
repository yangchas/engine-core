# Real provider cross-source audit — 2026-09-13

## Scope

This is a read-only server verification of the current `engine_core` archive
and the already deployed provider access paths.  It does not deploy Core,
replace `engine-next`, consume RabbitMQ, or write Redis/TDengine.

Core commit under test:

```text
bc2b2c10c77a5ae0e1719a5b083d7bc426f401d6
```

The exact source archive was copied to an isolated Cobra directory.  Archive
SHA-256:

```text
0f192fe0c1cbbd5a3a03ca5c37af4edfe8a1322afe62174075c647543f7f65d7
```

## Linux verification

Runtime: Cobra `Python 3.12.3` shared engine-next virtual environment.

```text
pytest -q -p no:cacheprovider  306 passed in 1.69s
compileall                    PASS
```

The local Windows run of the same archive source also passed `306` tests.

## Production service and storage snapshot

At 2026-09-13 22:35 CST:

```text
engine-next   active, MainPID=3181295, NRestarts=0
t1-v2-live    active, MainPID=2878024, NRestarts=0
/dev/root     19G total, 15G used, 3.1G available (83%)
memory        7.2Gi total, 4.3Gi available
swap          0B
```

The Core archive was not installed as a service.

## Real TD provider checks

### Auction projection

The existing `taos` client path executed a bounded `SELECT` against
`market_data1.auction_snapshot_v2` for `2026-09-10 / 600519`, tags `0920`,
`0924`, and `0925`.

```text
result              PARTIAL (0920→0924), READY (0924→0925)
business anchors    preserved as 09:20 / 09:24 / 09:25
source timestamps   preserved (09:20:03.323, 09:24:10.365, 09:25:06.078)
side effects        TD SELECT only
artifact SHA-256    fd4705420bf66049e1186c9ffbade801d7524e0b15738384a49f03717f2125b8
```

The result remains `FACT_ONLY`; it does not prove Rabbit batch membership or
the production finalization trigger.

### Previous-day data

`TDPreviousDayStatsProvider` reused the bounded per-symbol `daily_kline`
access shape for `trade_date=2026-09-10`, with the Calendar deriving
`previous_trade_date=2026-09-09`.

```text
rows                         3
requested symbols            000001, 300750, 600519
actual trade date            2026-09-09
result                       UNAVAILABLE
reason                       historical available_at is unknown
side effects                 TD SELECT only
artifact SHA-256             87256c0f1c24c4a75f0ddaa62781ca91f3cb70b32c68190a712074059cb9de59
```

This is the expected fail-closed result.  The query observation time is not
used as a fabricated historical availability time.

## Third-party connector probe

The probe reused the deployed `engine_next` connector implementations from
release `e272842c8f490f55a1b017badb71e71904ce008e` rather than creating new
connection code.

```text
connector connection checks     6/6 PASS
BaoStock daily date contract    PASS (request/response date match)
Kaipan hot plates               OBSERVED
Kaipan yesterday limit pool     OBSERVED
Kaipan ban reasons              OBSERVED
Wencai limit truth              OBSERVED; no structured historical date
THS hot rank                    OBSERVED; no historical date contract
artifact SHA-256                11e2c95df210d1fecca38a047444ac49ae2507f852a2341effa020769dc4ba16
```

Only the BaoStock result currently has evidence suitable for a dated runtime
contract.  The other sources remain useful as connectivity/oracle evidence,
but are not admitted to historical Replay runtime input.

## Redis availability

The Sunday probe found no `q2:active:*` keys.  Therefore no live Q2 snapshot
or real opening-fact result is claimed by this audit.  A missing weekend
projection is recorded as unavailable, not converted into a fresh or empty
success result.

## Conclusion

```text
core_linux_exact_suite                  PASS
real_td_auction_read_path               PASS (fact-only)
real_td_previous_day_read_path          PASS (fail-closed UNAVAILABLE)
legacy third-party connectivity         PASS 6/6
historical runtime authority            PARTIAL / UNKNOWN by source
live Redis Q2 on Sunday                 UNAVAILABLE
production replacement readiness       NOT CLAIMED
```

This evidence closes a real-provider verification slice only.  It does not
authorize production deployment, strategy migration, Rabbit takeover, or
loosening of the temporal data guard.
