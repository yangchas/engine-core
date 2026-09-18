# Legacy active consumer read-only probe (2026-09-18 16:45 CST)

## Scope

The exact deployed `engine-next@20260903_e272842` context path was invoked
through `examples/run_engine_next_context_probe.py` for bounded symbols
`000001`, `000002`, and `600519`, with a simulated business time of
`2026-09-18 09:26:00+08:00`. The probe disables known write/recovery hooks and
uses `GuardRedis`; it does not consume Rabbit, write Redis/TD, send a report,
or trigger an effect.

Remote artifact:

```text
tmp/real-reference-20260918/legacy-context-probe-1645.json
sha256=e8259304091c8c94f1059c5b6d225bbc36ebda199a9e3be3946b37f9158e848d
```

## Observed result

```text
phase=auction
snapshot_count=3
read_only=true
guard_writes=[]
legacy_fact_rows=3
```

The old context path returned real Redis Q2-derived values and executed the
legacy pure `build_auction_plate_bucket_stats` consumer. The bounded output
included auction amounts `4,333,500`, `696,600`, and `14,271,787` yuan and
expectation labels `observe/observe/noise`. The plate strings in this old
release were mojibake in the returned payload; they are preserved as observed
and are not treated as valid Core mapping evidence.

The key temporal finding is:

```text
future_source_timestamp=true
latest_quote_age_seconds=0
legacy_future_timestamp_handling=CLAMPED_TO_ZERO_AGE
```

The simulated 09:26 cutoff was earlier than the Redis Q2 source timestamps
returned by the current store. The old consumer therefore clamps the age to
zero instead of rejecting the future observation. Core must not copy this
behavior: a future source timestamp remains a temporal admission failure, not
fresh data.

Opening behavior rows were emitted as `OBSERVED` (`mixed`, `low_open_repair`,
`mixed`), but their rule status remains `UNKNOWN` and is not migrated.

## Migration conclusion

```text
LEGACY_ACTIVE_READ_PATH = OBSERVED
LEGACY_PLATE_BUCKET_OUTPUT = OBSERVED_ONLY
LEGACY_FUTURE_TIMESTAMP_HANDLING = UNSAFE_FOR_CORE_REUSE
CORE_PLATE_STRATEGY_PARITY = NOT_PROVEN
```

This probe is evidence for the Gate B audit, not a strategy oracle. It does
not authorize migration of plate thresholds, leader counts, opening labels,
or old future-time handling.
