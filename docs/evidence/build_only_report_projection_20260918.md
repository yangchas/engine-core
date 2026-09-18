# Build-only fact report projection — 2026-09-18

## Change

Commit `69f8535` adds `engine_core.reporting.build_auction_fact_report()` and
the immutable `AuctionFactReportArtifact`. It is a narrow presentation
projection from an already computed `AuctionFactShadow`; it does not add a
provider, scheduler, delivery owner, or strategy rule.

The projection preserves the old report boundary that was proven by the
legacy audit:

- explicit data origin;
- `COMPLETE`, `PARTIAL`, and `DATA_UNAVAILABLE` status;
- missing values remain unavailable;
- semantic and evidence hashes are separate;
- evidence/source-time range stays in provenance;
- no strategy conclusion or strategy phrase is generated.

It deliberately does not perform Redis/TD reads, recovery, backfill, Redis
claim/dedupe, SMTP, webhook, notification, or effect operations.

## Verification

Local:

```text
pytest -q -p no:cacheprovider: 474 passed
compileall: PASS
git diff --check: PASS
```

Cobra-ion isolated validation directory:

```text
/home/exedev/validation/engine-core-6511981-v1
Python: /home/exedev/services/engine-next/shared/venv/bin/python
pytest: 474 passed
compileall: PASS
```

Changed-file SHA-256 matched between local and Cobra-ion:

```text
src/engine_core/reporting.py  5b8f00e05472404d8e242b349819f0d6eac01bf6c1888a6adc14ede8474017ec
src/engine_core/__init__.py   7b98be93d60836c1b67e13ddf5c7361b3779d08bca642167d470fb1684e7fc5b
tests/test_reporting.py       9e9761b0f49318e6c2dfec5a4356eba9105731a685d7506e1efc17dc67d362c9
```

This is offline/isolated verification only. It does not change the production
owner decision: `engine-next` remains the report/effect owner, and live Core
replacement is still blocked by stale Q2 and TD storage pressure.
