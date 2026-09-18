# M2 Legacy / Core Differential Evidence — 2026-09-18

## Real source probes

All probes ran on `cobra-ion` against the deployed
`engine-next@20260903_e272842` release and the isolated Core validation copy.
They were read-only and bounded to `000001`, `000002`, and `600519`.

### Legacy auction loader

```text
source                 redis_snapshots
0920 / 0924 / 0925     200 / 200 / 200 rows
total                  600 rows
duplicate row keys     none
guard writes           none
selected bounded rows  none (symbols were outside the TopN projection)
```

This is valid evidence of the old loader's TopN projection, not a complete
market snapshot and not a failure of the Core path.

### Legacy context

The legacy context returned bounded rows for all three symbols and exposed its
auction amount/current-percent/plate fields. It also observed source Q2
timestamps later than the simulated 09:26 cutoff and reported
`latest_quote_age_seconds=0` by clamping the future age. This behavior is
recorded as legacy evidence only; Core must retain fail-closed future-source
handling.

### Core auction Engine shadow

The existing read-only Core TD `auction_snapshot_v2` shadow processed six
signals and three fact-only strategy results per symbol:

```text
000001  direct/engine semantic hash equal, PARTIAL/FACT_ONLY
000002  direct/engine semantic hash equal, PARTIAL/FACT_ONLY
600519  direct/engine semantic hash equal, PARTIAL/FACT_ONLY
```

This closes only wheel-to-Engine composition for the TD projection authority.

## Authority boundary

Legacy `context.auction_amount` is sourced from the legacy Q2/context
projection, while the Core shadow uses TD `auction_snapshot_v2` rows. They are
not the same authority or as-of cohort, so numeric differences are
`NOT_COMPARABLE` until an explicit same-source/as-of join exists. In
particular, `amount_yuan`/current Q2 must not silently replace
`auction_amount_yuan` from the auction projection.

## Evidence artifacts

```text
loader.json       sha256= c54d3e78d2d9082c09d1396ae921b71d048a17960a79e941181ea9b5b1897b3d
context.json      sha256= 9d1908c4a4b47be1e7ff5a2f4dbe74cf20c72dcc8b136d947765b4eaf9075c22
core/000001.json  sha256= 49b79fcfc2f758a4e110f8e2a25cad42141ee508a683a177c3514a115cdbcda5
core/000002.json  sha256= 838452a781a5e3fb54600ec2550a74868c481968c1f629d1a6ae90d222a63017
core/600519.json  sha256= 8d9bed2f4860ff211540031e43619ba0be6bb4d4979fc8fadef9988e0c10df7f
```

Remote evidence directory:

```text
/home/exedev/validation/m2-legacy-differential-20260918
```

No production service, Redis/TD writer, Rabbit consumer/ACK, notification,
or effect was changed.

## Next boundary

Before migrating a strategy, Core needs an explicit authority matrix for the
auction projection versus current Q2 and a same-source/as-of differential
fixture. TopN loader coverage must not be mistaken for full-universe runtime
coverage.
