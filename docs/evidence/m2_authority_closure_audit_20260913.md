# M2 reference-data authority closure audit — 2026-09-13

## Scope and source identity

Read-only audit of the current cobra-ion release:

```text
release: /home/exedev/services/engine-next/releases/20260903_e272842
commit:  e272842c8f490f55a1b017badb71e71904ce008e
runtime: engine-next and t1-v2-live active
```

The audit used source inspection and Redis `TYPE/HLEN/HSCAN/GET` only.  No
producer, writer, repair, Redis/TD write, Rabbit ACK, notification or effect
was invoked.

## Writer and reader findings

### Hot plates

`IntradayDataHub.fetch_hot_plates()` writes
`cache:hot_plates:{trade_date}` and
`cache:hot_plates_meta:{trade_date}`.  The current metadata records source,
trade date, row counts, `success`, `cache_preserved`, `updated_at*` and
`last_attempt_at*`, but does not record a schema version, `available_at_ms`,
field units, or a payload hash.

`app_main._load_runtime_readiness()` treats a non-empty hash plus matching
row count/date and positive `updated_at_ts` as ready.  That is a cache
freshness/readiness check, not historical knowledge availability evidence.

### Yesterday limit pool

`IntradayDataHub.fetch_yest_limit_pool()` writes the same update/attempt
metadata and may preserve an old payload when a fetch returns no rows.  The
current release can add a payload hash for a successful/preserved pool, but it
still has no verified `available_at_ms`, schema version, or field-unit map.

`prior_limit_cache_contract.evaluate_prior_limit_authority()` correctly checks
trade date, source, row count and payload hash before the legacy reader accepts
the cache.  Its `cache_preserved=true` branch only proves a retained payload
with a positive update timestamp; it does not prove when the retained facts
became historically available.

## Observed Redis partitions

Bounded read-only scans of the existing date partitions returned:

| date | hot plates | hot rank | yesterday limit pool | limit truth |
|---|---:|---:|---:|---:|
| 2026-09-08 | 50 | 100 | 73 | 73 |
| 2026-09-09 | 50 | 100 | 48 | absent |
| 2026-09-10 | 50 | 100 | 35 | absent |
| 2026-09-11 | 50 | 100 | absent | absent |

Present keys were Redis hashes and bounded `HSCAN` counts matched `HLEN`.
Sample rows carried the requested `trade_date` and source labels.  The
`yest_limit_pool` `turnover` values were large integer amounts (for example
`169552503.0` and `786526695.0`), while the legacy schema describes that field
as a percentage.  Until the producer/consumer unit contract is independently
verified, core must keep `turnover` as unknown-unit data and not convert it.

## Core classification

The current core providers correctly classify the real caches as:

```text
hot_plates:       UNAVAILABLE / available_at_unknown
yest_limit_pool:  UNAVAILABLE / available_at_unknown + turnover_unit_unknown
```

This is a correct fail-closed result.  The observed update timestamps establish
when the cache was changed or inspected by the writer; they do not establish a
historical `available_at_ms` for replay knowledge cutoffs.

## Decision

```text
M2_AUTHORITY_CLOSURE = BLOCKED
```

A minimal writer contract change is required before these datasets can become
runtime-authoritative: versioned metadata with a verified availability field,
explicit field units, and a stable payload/count/date/source lineage.  The
existing controlled writer candidate is not deployed or shadow-verified on
Cobra, so no production authority claim is made here.

The next permitted action is a candidate-only writer change followed by the
existing verification chain.  Until a real Cobra shadow observes the new
metadata, legacy metadata remains fail-closed and the candidate modules remain
outside `DeterministicEngine`.
