# A2 summary build-only report integration

## Scope

Commit `adaf831` extends the narrow `AuctionFactReportArtifact` projection with
an optional, already-normalized `AuctionMarketSummaryFact`. This is a build-only
composition step. It does not read Redis/TDengine, perform recovery, claim a
delivery slot, send SMTP/Webhook notifications, or evaluate a strategy.

## Contract

- The summary must be an explicit `AuctionMarketSummaryFact`; raw provider maps
  are rejected at the report boundary.
- Canonical summary fields retain count/yuan units and explicit zero values.
- Missing or invalid summary fields remain visible through the summary status;
  no report layer fills them.
- Summary `content_hash` participates in the report semantic identity.
- Summary `evidence_hash` and the fact evidence remain in report provenance;
  source identity and observation time do not alter the semantic report hash.
- Text output contains objective A2 fields only; it does not contain strategy
  conclusions such as BUY/PASS.

## Verification

- Local Windows: `481 passed` and `python -m compileall -q src tests`.
- Cobra-ion isolated validation copy: the same test suite passed (`481`) and
  compileall passed under the production Python 3.12 environment.
- The tracked A2 fixture is a frozen extraction from real 2026-09-14 capture
  evidence; it is not synthetic market rows.
- No production service was restarted and no Redis/TD/Rabbit/effect path was
  written or changed.

## Parity boundary

This closes only A2 summary normalization and its build-only projection. It
does not establish parity for plate rows, locked orders, mapping/anchor
projections, report delivery lifecycle, or legacy strategy thresholds. The
legacy `engine-next` report/delivery path remains the production owner.
