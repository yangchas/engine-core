# Engine integration contract fixes — 2026-09-10

## Verified commit

```text
branch: codex/feature-session-engine-integration
commit: 291d64d511c05fc9140765819fb837e817c7846d
archive_sha256: 28edba4037fef4293d51e2fa66a942a6267fc88a48e02a90b131e2f645b43203
```

The archive was extracted to a new temporary directory on `cobra-ion`; no
production checkout or service was modified.

## Local and cobra-ion verification

```text
local Python tests:       191 passed
cobra-ion Python 3.12:    191 passed
compileall:               PASS (both environments)
git diff --check:          PASS
```

The local and remote test suites ran from the same archived commit and fixture
set.  The remote archive SHA matched the local SHA.

## Contract fixes covered

```text
Provider access exception remains DataStatus.ERROR, not MISSING
Provider observed_at_ms is sampled after the legacy access callable returns
engine_next context probe delegates phase selection to infer_run_phase()
read-only Redis probe permits HLEN used by the intraday context path
auction shadow snapshot/segment quality is derived from required P/M/RB/RA
```

## Real read-only checks on cobra-ion

The corrected context probe ran against the deployed `engine_next` release at
`2026-09-10 09:35:00 Asia/Shanghai` for `600519`:

```text
phase:                  intraday
snapshot_count:         1
guard_writes:           []
read_only:              true
```

The probe observed a future source timestamp relative to the supplied probe
time and reported the legacy handling (`CLAMPED_TO_ZERO_AGE`) unchanged.  This
is recorded as legacy evidence; this change does not reinterpret that behavior.

The real TD `auction_snapshot_v2` shadow for `600519` at the same trade date
produced:

```text
0920→0924 coverage:     PARTIAL (px_milli absent at 0920)
0924→0925 coverage:     READY
shadow status:          PARTIAL / FACT_ONLY
read_only:              true
```

The source timestamp values were preserved as observed; business anchors remain
09:20, 09:24 and 09:25.  No Redis/TD write, Rabbit action, repair, notification
or strategy effect was invoked.

## Residual evidence gaps

```text
Rabbit arrival and runtime batch membership are still UNKNOWN.
The legacy future-source timestamp clamp remains an observed behavior.
Auction shadow is intentionally fact-only and does not migrate a formal strategy.
```
