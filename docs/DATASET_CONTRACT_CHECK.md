# Dataset contract checker

Executable form of `fw/design/24-DATASET-CONTRACT.md`. Read-only; never writes to the checked root.

## Usage

```
python examples/check_dataset_contract.py --dataset daily_kline --date 2026-10-27 [--root ~/core-data] \
    [--version sha256-...] [--spec path.json] [--format json|text] [--out report.json]
```

`--date latest` uses the newest catalog date. Without `--version` the highest-quality version of the day is
checked (FORMAL > PROVISIONAL > DEGRADED, ties to the last appended). stdout is JSON (`ok`, `counts`, `stats`,
`violations`, `warnings`, `checks`); stderr carries a human summary. Library entry point:
`engine_core.data_pipeline.dataset_contract.check_partition(root, dataset, date, version=None, spec=None)`.

Exit codes: 0 all checks pass (WARN allowed), 1 at least one FAIL, 2 usage or spec error.

## Checks

| id | what |
|---|---|
| A0-A2c | partition exists/readable; path `<root>/<dataset>/trade_date=D/source=X/sha256-<16hex>/`; `source=` equals `manifest.provider` and is not a chain label (`>`); provider in spec `allowed_providers` |
| A3-A3d | manifest required fields (§4), `quality` subfields, `provider_attempts` item keys, `provider_chain` contains provider |
| A4-A7 | dataset/trade_date identity; `business_status` in FORMAL/PROVISIONAL/DEGRADED and `degraded` true iff DEGRADED; row_count agrees with rows and catalog; `sha256` = sha256 of the stored rows payload and `version` = `sha256-<first 16>` (nodes with `inputs` use the identity hash of storage.py) |
| A8-A10 | times parse with timezone; `available_at >= as_of` and `>= fetched_at`; no look-ahead: `available_at >= trade_date + earliest_available_local` (Asia/Shanghai) |
| A11 | `manifest.field_units[col]` equals the spec unit for every spec column that has a unit |
| B1-B3b | `symbol` 6-digit string; `date` column equals trade_date (required for every dataset, §3); extra `date_columns` equal trade_date; primary key unique; rows sorted by primary key |
| B4-B8 | no columns outside the spec / none missing; per column: type, nullability, all-null column, `min`/`max`, `enum`, `zero_suspicious` (0 where null belongs) |
| C1-C4 | `expected_basis` matches spec; coverage, expected_count, missing, suspended_in_universe recomputed from the same-date `symbol_universe` (active = `trade_status != "0"`) and compared with the manifest; no rows outside universe; `quality.dropped_outside_universe` and `dropped_duplicates` declared. Universe missing: WARN |
| D1 | re-serialising the parsed rows (canonical JSON, one per line) gives the stored bytes and sha256 |
| E1-E5 | checked version is best of the day; `ArtifactCatalog.locate` returns the best-quality version; `latest` is not PROVISIONAL/DEGRADED/SAMPLE and is FORMAL when a FORMAL version exists for that day; `latest.json` matches catalog.json. Stats list keys that exist only in lower-quality versions |

Not checked (no machine evidence in the partition): `source_dates` date evidence (field is optional in the
checker; add a spec flag when a dataset provides one), TD column mapping, independent cross-source validation.

## Spec files

`src/engine_core/data_pipeline/dataset_specs/<dataset>.json` (the checker is dataset-agnostic; all dataset
knowledge is here):

```
{ "dataset": "x", "status": "BASELINE" | "DRAFT: ...",
  "primary_key": ["symbol"], "symbol_column": "symbol" | null, "date_columns": ["trade_date"],
  "row_order": "primary_key", "earliest_available_local": "15:00" | null,
  "allowed_providers": ["..."]                      // optional
  "coverage": {"basis": "universe_active" | "none", "universe_dataset": "symbol_universe"},
  "columns": { "<col>": {"type": "str|float|int|bool|list|dict|date", "nullable": false,
       "unit": "yuan|percent|shares|...", "min": 0, "max": 1, "enum": [..],
       "zero_suspicious": true, "all_null_ok": false, "optional": false } } }
```

Units use the vocabulary of `registry/units.py`. daily_kline: price/amount `yuan`, `pct_chg`/turnover `percent`,
volume `shares`. `zero_suspicious` means 0 is flagged in that column (e.g. volume, prices); a column where 0 is a
real value (pct_chg, counts) does not set it.

## Writing a spec for a new dataset

1. Copy the closest spec, set `dataset`, `primary_key`, `symbol_column` (null for non-stock keys) and `coverage`.
2. List every column of the real latest partition with type/nullability/unit/range; run the checker; the
   `B4`/`B4b` checks tell you what you missed. Do not widen ranges to hide real violations.
3. Keep `status` as `DRAFT: ...` until the owning data card confirms it; set `BASELINE` only after a clean run
   against a real partition.
4. Specs are loaded from the source tree (`SPEC_DIR`); packaging them as package data is not configured here.

## Tests

`tests/test_dataset_contract.py`: positive case, one negative case per check family, CLI exit codes, and a
mutation suite (unit swap, symbol format, deleted manifest field, 0 for null, chain-label source, row_count)
run on both a synthetic partition and a conformant copy of the real 2026-10-27 daily_kline partition
(temp root; skipped when core-data is absent).

## daily_kline writer and legacy partitions (C576)

Single exit: `storage.finalize_daily_kline_contract_fields` (called at the end of `_prepare_daily_kline_manifest`,
which `ArtifactCatalog.commit` runs for every daily_kline write: provider chain, Q2 snapshot, degraded capture,
job runner). It writes `source`/directory = actual provider (a caller's chain label is kept only as
`declared_source`), `provider_chain` (list, always containing the provider), `degraded`, `field_units`,
`provider_attempts[].row_count` and `quality.dropped_outside_universe/dropped_duplicates`
(from `quality_notes.universe_excluded_rows/duplicate_rows`; `null` when the writer reports no drop counts, as in the
Q2 snapshot and degraded capture paths). A merged q2_final+wencai row set takes the provider that supplied most
field values (`field_sources`).

Rulings: `degraded` is true iff `business_status == DEGRADED` or the provider is not the spec's `primary_provider`
(baostock): a wencai/q2_final partition is PROVISIONAL (C575) and degraded. A `PARTIAL` business status (coverage
below 0.98, design/24 ruling of 2026-10-28) is degraded and sets `quality.partial=true`; A5 checks both, and the
writer (`finalize_daily_kline_contract_fields`) does both. E3: a PROVISIONAL/DEGRADED version may hold `latest` only while
no higher-quality version exists for that date or any newer date; a sample never may. Canonical rows (C579) use
pct_chg as a percent float and carry `turnover_pct` / `suspended` (docs/DAILY_KLINE.md, "Consumer evidence");
`dataset_specs/daily_kline.json` describes that layout (`provider_overrides`: q2_final has an all-null `open`;
wencai/q2_final have all-null optional `turnover_pct` and `suspended`). `daily_kline_legacy.json` describes the
earlier raw layout (same units, extra bookkeeping columns).

Historical partitions (for example the real 2026-10-27 `source=baostock>wencai>q2_final`) are never rewritten.
Their violations disappear when the next formal upgrade writes a new version. `--legacy-tolerant` is a read-only
switch: for a manifest without `provider_chain` it checks against the legacy spec and reports A2, A3, A3c, A3d, A5,
A11, C4 as WARN with a `LEGACY-WAIVED` prefix (`legacy_waived` in the report). Partitions that carry
`provider_chain` are checked exactly as before.

Known remaining failure: E3 for PROVISIONAL partitions that advance latest on purpose (C575 wencai same-day, Q2
snapshot at 15:05); the catalog policy and design/24 section 2 disagree and need a decision.
