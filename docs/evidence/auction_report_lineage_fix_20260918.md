# A2 report evidence-lineage fix

## Change

Commits `ec5dd37` and `5409cf5` close two narrow contract gaps in the A2
report slice:

1. `AuctionMarketSummaryFact.as_mapping()` now exposes the canonical
   `limit_up_seal_amount_yuan` field that is already part of the semantic
   contract and text projection.
2. `AuctionFactReportArtifact.evidence_hash` now includes the optional summary
   evidence hash and stable evidence references. A source/evidence change with
   identical business values therefore preserves `semantic_hash` but changes
   `evidence_hash`.
3. The summary evidence-reference payload is canonicalized as a direct stable
   tuple rather than an unnecessary nested tuple.

No provider, runtime, strategy, delivery, or production path was changed.

## Verification

- Local: `482 passed`; `python -m compileall -q src tests` passed.
- Cobra-ion isolated copy: Python `3.12.3`, `482 passed`, compileall passed;
  the four changed source/test file SHA-256 values matched local exactly.
- Cobra-ion verification environment was `TZ=CST`, POSIX locale; production
  `engine-next` and `t1-v2-live` remained active.
- The added regression test proves semantic identity remains equal while
  evidence identity changes when only summary evidence changes.
- The package remains build-only and fact-only; `engine-next` remains the
  production report/effect owner.
