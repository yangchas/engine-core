# Static data snapshots

## Current generation and observed snapshots (2026-10-29)

| Dataset | Current snapshot | Existing generator/source | Point-in-time limits |
|---|---|---|---|
| `plate_registry` | 2026-10-13, 709 rows, version `sha256-e60d77f986a69861` | Current Core artifact says `import_from_validation`; its source paths are `/home/exedev/validation/plate-registry-pit-20261013/registry.json` and `examples/data/plate_generic_factor_names.json`. `examples/build_plate_registry.py` can build the registry from Kaipan `个股板块.csv`, `板块.csv`, and a local dictionary, but no configured live full-registry fetcher is present. | Manifest `as_of=2026-10-16T08:50:00+08:00`, `available_at=2026-10-13T15:10:00+08:00`, `point_in_time_date=2026-10-13`. The snapshot is non-PIT for historical replay unless the selected immutable version was available by that replay cutoff. |
| `plate_members` | 2026-10-28, 89,943 rows, version `sha256-509339f55a225534` | `examples/run_data_jobs.py` calls `KaipanProvider.plate_members`; that provider calls Kaipan `getHisPlateIds` per plate code. `examples/fetch_kaipan_plate_members.py` is a lower-level capture tool. | Manifest `as_of` is date-only `2026-10-28`, provider fetch began at `2026-10-28T17:00:40Z`, and `available_at=2026-10-28T17:02:21.483253Z`. Membership is a snapshot and can change with constituent changes and source corrections; the date alone does not prove historical availability. |
| `stock_static` / `name_map` | Before C573: 2026-10-14, 5,875 rows, version `sha256-ec9dd58b7cab36a2`. After the temporary C573 Guardian event: 2026-10-29, 5,224 rows, version `sha256-d2f57645aafb76b7`, `business_status=PROVISIONAL`. | The old artifact provenance says `import_from_validation`: stock names dated 2026-10-13, Kaipan caps dated 2026-10-13, and an F10 refresh dated 2026-10-14. The new snapshot is built from the 2026-10-28 `symbol_universe` plus one Baostock `query_stock_basic` response. `name_map` remains a catalog alias/view, not a separate artifact. | New manifest `as_of=2026-10-29T04:58:00.291131+08:00`, `available_at=2026-10-28T20:58:56.682106Z`, source date echo absent. It is a current-reference observation, not PIT truth. Consumers must retain its version and only use it after `available_at`; the 2026-10-14 version remains immutable. |

The existing `plate_registry` script reads local inputs and writes JSON/CSV/NPZ files. The scheduled member job reads the currently selected registry codes and requests membership snapshots. The `stock_static` artifact was assembled by the validation importer; its source manifest records imported inputs, not an ongoing provider refresh. Thus these catalog entries did not establish automatic refresh before C573. The `datasets.registry.json` entry for `plate_registry` previously had `cadence=UNKNOWN` and `late_policy.mode=manual`; `name_map` also documented a manual import. The latest `plate_members` snapshot is newer because the existing Kaipan job ran independently.

## Snapshot contract

The partition key is `(dataset, trade_date, source, version)`. A refresh writes a new immutable version under the target snapshot date; it never rewrites an existing version. Every emitted row has `date=trade_date`. Manifests record the target date, timezone-aware retrieval `as_of`, `available_at`, provider, source dates/evidence, attempts, version, row count, and quality counts. `as_of` means when the builder observed the inputs; each `source_dates` entry records the provider's own date echo when available, or explicitly records that no date echo was supplied. `available_at` is the time the version became consumable after commit.

Historical consumers select the newest version whose `available_at` is no later than the consumer cutoff, and retain the version/source metadata with the result. A snapshot carrying a later or unknown source date cannot be treated as historical truth merely because its `trade_date` names an earlier day. `plate_registry` and `stock_static` are current-state reference snapshots; `plate_members` is a dated membership snapshot, not a guarantee of PIT membership history.

### Row fields and identity

- `plate_registry`: primary key `plate_code` (same identity as legacy `code`); `name`, `class`, `family`, nullable `parent`, `children`, `flags`, `generic_factor`, member `size`, `date`, and row availability evidence. Plate codes are the Kaipan 801xxx identities; names are display labels only.
- `plate_members`: primary key `(plate_code, symbol)`; `plate_class`, `plate_member_label`, and `date`. `trade_date` is the source's membership date and is retained as source evidence. `symbol` is a six-digit A-share code. Membership whose `plate_code` is absent from the selected registry is retained and counted as an identity mismatch; it is never silently discarded. Registry-only codes are separately counted.
- `stock_static` (`name_map` view): primary key `symbol`; `name`, `industry`, nullable `listing_date`, nullable `is_new_stock`, nullable `is_st`, `exchange`, `market_board`, `limit_width_pct` (percentage points, e.g. 10 means 10%), `float_cap_yuan` and `total_cap_yuan` (yuan), `cap_as_of`, `date`, `source_quality`, and `available_at`. Exchange uses the exchange prefix in the Baostock or universe qualified code; it is `UNKNOWN` when neither source provides one. `market_board` is inferred from the six-digit code prefix. ST is an observation from the selected name, not a market-rule conclusion; when no name exists `is_st` is null. Missing fields remain null and are counted, never replaced with zero.

## Refresh policy and validation

`plate_registry` and `stock_static` are scheduled for one weekly PRE_MARKET refresh, plus a refresh request when the daily `symbol_universe` introduces a symbol absent from the newest static snapshot; snapshots older than eight calendar days produce a stale alert. Startup after the trigger window catches up the pending refresh. Fetch retries use `policy_05` (three attempts, 60-second exponential backoff capped at ten minutes, until 15:00). A fallback runs only when a fallback source is explicitly configured. `plate_members` refreshes weekly and when the universe adds a security, using the newest registry identity set and a 400-request Kaipan cap per attempt. Incomplete member fetches fail and retry; they are not committed as complete. The scheduler policy is configuration-driven; provider capability and request budget remain explicit.

C573 routes the new event through the existing `premarket_data` Guardian handler name so Python and Go schedule validators continue to accept it. The handler only routes to `examples/static_data_refresh.py` when the event carries `static_data_refresh` arguments. A complete live `plate_registry` source is still not configured: the handler records `SOURCE_NOT_CONFIGURED` and stale age, and does not treat hot-board rankings or the previous version as a refresh. Stock refresh reads the latest Core `symbol_universe`, performs one Baostock `query_stock_basic`, commits with `ArtifactCatalog`, reads the immutable version back, and checks it with C570. Each successful static refresh includes its retrieval timestamp in manifest inputs so identical rows from a later retrieval receive a new version.

### C573 live refresh and cross-check (2026-10-29)

The temporary Guardian event triggered at 04:58 with a temporary state root and the real immutable Core catalog. It skipped fresh `plate_members`, reported `SOURCE_NOT_CONFIGURED:plate_registry` and `STALE_ALERT age_days=16`, then refreshed `stock_static`. Readback returned 5,224 rows, version `sha256-d2f57645aafb76b7`; C570 passed with 55 PASS, 0 FAIL, 7 WARN, 1 SKIP. Seven warnings are the intentionally null industry, new-stock flag, limit width, float/total cap, cap date and PIT date fields. The Baostock response had 5,564 type-1 rows; against 5,224 universe symbols, missing basic records, name mismatches and ST mismatches were all zero. The provider supplies no source-date echo, so the snapshot remains PROVISIONAL. Two earlier pre-fix refresh attempts also left immutable 2026-10-29 versions in Core; explicit checks report only E2 (`locate_returns_best_quality`) for those versions because default `locate` now resolves to the final version. The final version is the current latest and passes C570.

The stored 2026-10-28 membership snapshot has 89,943 rows across 706 plate codes. The 2026-10-13 registry has 709 codes: zero member rows use an unregistered code; three registry-only codes are `801058`, `801961`, and `803003`. Ten fresh Kaipan `getHisPlateIds` responses matched their stored plate rows exactly:

| Plate code | Kaipan `Count` / `list` rows | Stored rows | Difference |
|---|---:|---:|---:|
| 801050 | 20 | 20 | 0 |
| 801085 | 1,530 | 1,530 | 0 |
| 801182 | 118 | 118 | 0 |
| 801200 | 59 | 59 | 0 |
| 801260 | 203 | 203 | 0 |
| 801295 | 43 | 43 | 0 |
| 801376 | 84 | 84 | 0 |
| 801535 | 25 | 25 | 0 |
| 801553 | 362 | 362 | 0 |
| 801873 | 33 | 33 | 0 |

The 801085 response's auxiliary `Stocks` field is capped at 100 even though `Count` and `list` each contain 1,530; comparisons use `list`. C573 made 12 Kaipan requests total (10 board comparisons and two exploratory calls), zero Wencai requests, and five Baostock `query_stock_basic` requests (two raw fixture captures and three scheduled refresh attempts, including two before the latest-pointer fix). The final attempt passed C570. No full live registry source or Wencai comparison was available, so registry refresh remains degraded and is an open item.

Stock name/ST observations are compared across `symbol_universe` and Baostock `query_stock_basic`; plate-member code coverage is compared with the registry and ten board samples are checked against fresh Kaipan responses. A source that does not expose a full registry or a date echo is recorded as such; no hot-board ranking is treated as a full registry, and no unavailable historical date is inferred from retrieval time.
