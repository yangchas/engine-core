# Gate B Anchor Delta Fact Evidence — 2026-09-14

## Scope

This is the first migrated Gate-B rule.  It migrates only the audited pure
per-symbol anchor-delta helper from the production release:

```text
engine_next.runtime.auction_shadow.build_anchor_shadow_evidence
engine_next.runtime.auction_shadow.build_anchor_delta_evidence
```

The migration covers both adjacent transitions:

```text
0920 -> 0924
0924 -> 0925
```

It does not migrate plate scoring, leader/follower rules, `turn_strong`,
strategy thresholds, reporting, recovery, or effects.

## Exact source and Core identities

```text
Production release: /home/exedev/services/engine-next/releases/20260903_e272842
Legacy auction_shadow.py SHA-256: c530b41b10643204eb89ce3b73b720e01caade4936643aa215948f686ab90af0
Core implementation commit: fa555b1 (follow-up normalization fix: `697b6ba`)
Final Core commit used for verification: 697b6ba2e5cfc2ad9dcda3d3851620071d3007fc
Final Core archive SHA-256: 4dd239df56d2d4407f93626dc16cc1ca0c7e7d9c0a209c50f3ef174aa4aed513
Local/Cobra suite: 351 passed
compileall: PASS
```

## Contract

Inputs are normalized anchor mappings with strict semantic fields:

```text
symbol
tag
price_milli (or price converted to milli)
auction_amount_yuan (or amount/am)
bid_amount_yuan (or bid_amount/br)
ask_amount_yuan (or ask_amount/ar)
```

Missing fields remain `unavailable`; invalid finite/non-negative checks and
`price > 0` follow the audited legacy helper.  The output retains the legacy
fact fields (`amount_delta_yuan`, `price_delta_milli`, bid/ask deltas,
pressure, ratio, withdrawal, direction, status, labels and reference bucket).
Pressure is a resting-order proxy, not net capital flow.

## Real TD differential

On Cobra-ion, the same read-only TD query was passed to both the production
helper and the Core helper after the same one-time row normalization.  JSON
was canonicalized with sorted keys and compact separators; no tolerance was
used.

For the real 2026-09-09 / 600519 rows:

```text
0920 -> 0924: exact=True
canonical output SHA-256: c719e4103be62442bda5f824c92932b2d6881a10499862650841e9fff149ecd7

0924 -> 0925: exact=True
canonical output SHA-256: 16b81fec538305a692b29d33c0fceda2fb7fd7eed5d2b77c4fe7b015d9a9d488
```

For the current 2026-09-14 / 600519 rows, both implementations also
returned the same `unavailable` facts because the required price fields were
not present in the TD projection rows:

```text
0920 -> 0924: exact=True
canonical output SHA-256: 29328625675e01136647f8223b5283eda5b8759300576f6dfcfe99a252f29800

0924 -> 0925: exact=True
canonical output SHA-256: dfb99ad5fc5c22197a51727a4602cbbf51a83290ee169b4e107084af997db6e1
```

The same real TD read was also executed through
`examples/run_anchor_delta_shadow.py`; it emitted a read-only evidence
artifact for each transition.

## Tests

The Core wheel has 17 focused tests covering:

```text
Golden: both transitions and verified legacy values
Boundary: missing anchor, aliases, balanced state, amount bucket edges
Adversarial: ask absent, missing price, zero/negative price, non-finite amount
Native TD adapter: provider-native timestamps retained as evidence only
```

Full repository verification on local Windows and Cobra-ion Python 3.12.3:

```text
351 passed
compileall PASS
```

## Status

```text
AnchorDeltaFactV1 pure wheel:       VERIFIED
Legacy differential (real TD):     VERIFIED for the two tested transitions
Strategy migration:                NOT DONE
Plate/global completeness parity:  NOT DONE
Production replacement readiness:  NOT READY
```

The next step is to audit the state lifecycle and choose one additional
verified rule only if its active production consumer and fixtures are proven.
No new generic strategy framework is introduced.
