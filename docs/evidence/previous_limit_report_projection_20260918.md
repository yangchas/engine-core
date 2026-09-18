# Previous limit-up structure report projection

Commit `6a56388` connects the guarded `PreviousDayLimitStructureFact` to the
existing build-only `AuctionFactReportArtifact` as an optional fact section.
The projection exposes only the previous-session structural fields:

- previous trade date
- pool row count
- highest board height and symbols
- board-height distribution

It does not claim current-session return feedback, plate strength, locked-order
facts, mapping readiness, strategy thresholds, or delivery ownership. The
structure semantic hash and evidence lineage are both carried through the
report artifact; provider I/O and all side effects remain outside this module.

Verification:

- Local: `487 passed`, compileall PASS.
- Cobra-ion isolated Python 3.12.3 copy: `487 passed`, compileall PASS.
- `reporting.py` and `test_reporting.py` SHA-256 matched local exactly.
- No production service, Redis/TD data, Rabbit ACK, notifier, or effect path
  was changed.
