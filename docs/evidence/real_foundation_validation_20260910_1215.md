# Real foundation validation — 2026-09-10 12:15

## Runtime

The validation ran on `cobra-ion` from the isolated checkout of core commit
`5d39f37611676ea6bff81ec2b43afe31835c81fd`.  The existing engine-next
connectors and Python 3.12 runtime were reused.  All requests were bounded
and read-only; no Rabbit consumer, Redis writer, TD writer, repair, report or
notification path was started.

## Redis Q2

The real `q2:active:20260910` projection was read through the existing core
adapter at `2026-09-10T12:16:43+08:00`:

```text
requested / received:           5219 / 5219
row coverage:                   1.0
status:                         STALE
stale symbols:                  5219
newest source lag:              2801 seconds
repeat engine result hash:      equal
read-only key count:            5220
```

The source was stale at the observation time, and the core correctly kept the
quality status as `STALE`.  This is a data-state observation, not a failure of
the decoder or calculation path.

## Existing third-party connectivity

The bounded `run_real_reference_probe.py` used the deployed connector paths
with audit date `2026-09-09`, symbol `600519`, and at most three rows per
query:

| Source | Connection | Contract | Rows | Date evidence |
|---|---|---|---:|---|
| Baostock daily kline | PASS | PASS | 1 | requested and returned date match |
| Kaipan hot plates | PASS | OBSERVED | 3 | response not self-dated |
| Kaipan yesterday limit pool | PASS | OBSERVED | 3 | response date not required by current connector |
| Kaipan ban reasons | PASS | OBSERVED | 1 | no structured source date |
| Wencai limit truth | PASS | OBSERVED | 3 | current query has no structured date |
| THS hot rank | PASS | OBSERVED | 3 | current query has no date |

The connections are usable through the existing access behavior.  `OBSERVED`
does not promote those results to historical replay input; a verified
`available_at` or an explicitly dated response is still required by the
temporal data guard.

## Evidence and limitations

Raw JSON was captured in the isolated server directory
`/tmp/core-real-validation-20260910-1215/` and copied to local temporary
files for inspection.  The current Q2 view is not an atomic historical
snapshot, and the third-party probes are connectivity/contract checks rather
than strategy truth.  Chinese display text in the remote terminal is not used
as a semantic contract.
