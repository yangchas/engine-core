# Legacy read-path probe — 2026-09-13

## Scope

This is a bounded, read-only probe of the deployed `engine_next` release
`e272842c8f490f55a1b017badb71e71904ce008e` on `cobra-ion`.  It uses the
existing shared Python 3.12 environment and a Redis write guard.  It does not
consume RabbitMQ, call network recovery, write Redis/TDengine, send mail, or
run a strategy.

## Commands and evidence

The exact `engine_core` probe archive was built from commit
`247ae83c398e31728d4569922e1cd80362d0e7cc` and unpacked under `/tmp` on the
server.  The following probes were run against the current release:

```text
run_engine_next_auction_loader_probe.py
  trade_date=2026-09-10
  tags=0920,0924,0925
  symbols=600519,000001,000002

run_engine_next_context_probe.py
  trade_date=2026-09-10
  previous_trade_date=2026-09-09
  phase=auction
  now=09:26:00
  symbols=600519,000001,000002
```

The probe artifacts were written to server `/tmp` only:

```text
engine-next-loader-20260913.json
  sha256= b517937fdfaf441c7f100bac802098e62889caa7055ea7e126c87190cbb787ec

engine-next-context-20260913.json
  sha256= 153a9791d1e9a5093168cd4f9838d836ba75611fbf5ce933057b1ed6452be8e7
```

## Observed results

### Auction snapshot loader

The legacy `load_auction_snapshots()` path returned:

```text
row_count=0
row_count_by_tag={0920: 0, 0924: 0, 0925: 0}
source=empty
duplicate_row_keys=[]
guard_writes=[]
read_only=true
```

This does not prove that the producer never wrote snapshots.  It only proves
that the current release's reader found no usable `top_amount` rows for this
date at probe time.  It is therefore `OBSERVED`, not a historical snapshot
oracle.

### Intraday context reader

The context path returned three rows, but all selected rows had
`auction_amount=0.0` and `current_pct=0.0`; `latest_quote_timestamp_ms=0` and
`latest_quote_age_seconds=null`.  The existing pure fact call classified the
rows as `observe`/`noise` with `leader_count=1`.

The plate strings in the JSON output were mojibake on the remote runtime (for
example `������`).  This is recorded as an encoding/identity observation only;
the probe does not infer the intended plate names and does not treat the
legacy output as a strategy oracle.

The context probe also reported:

```text
guard_writes=[]
read_only=true
side_effect_boundary=real Redis reads plus legacy pure fact call;
known cache, network, recovery, writer, notification and effect hooks disabled
```

## Interpretation and migration impact

| Item | Status | Meaning |
|---|---|---|
| Legacy reader path is callable | OBSERVED | The release imports and executes the path. |
| Historical 0920/0924/0925 rows are available for 2026-09-10 | UNKNOWN | Reader returned an empty result; retention/key lifecycle is not proven. |
| Zero auction fields represent valid facts | UNKNOWN | Do not convert them to `READY` or use as a legacy oracle. |
| Plate identity/encoding is safe for migration | UNKNOWN | Remote output is not a stable canonical identity. |
| Probe is side-effect free | PASS | Redis guard observed no writes; no producer action was invoked. |

This evidence does **not** justify changing `engine_next`, adding a fallback,
or migrating a strategy.  It narrows the next investigation to the real
producer/writer evidence and to a same-input legacy oracle captured during a
trading session.

## Next bounded action

On the next trading day, capture a small set of live 0920/0924/0925 Redis
snapshot keys and the corresponding Q2 observation for the same symbols before
running this reader again.  If the reader still returns an empty result while
the keys contain data, inspect the release's key/schema mapping.  If the
fields remain zero or mojibake, keep the result `UNKNOWN` and do not promote it
to a core contract.

