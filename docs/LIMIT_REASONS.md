# Limit reasons

`limit_reasons` is the cold, per-stock explanation attached to the previous session's limit-up pool. Its partition
`date` is the trade date being described. In the premarket run on D, the partition is D-1, so a reason with
`event_date > date` is future information and is excluded. A first-board reason that cannot be recovered is attributed
from the latest earlier Type 0 record and marked `DEGRADED` / `HISTORICAL_ATTRIBUTION`.

## 既定规则

These are the existing rules from `RULES.md` §6, the KAIPAN-FETCH / KAIPAN-FULLHIST and NO-LOOKAHEAD facts, and
`design/04-L1-BOARDS.md` / `design/16-FIELD-CATALOG.md`; this section records them for the dataset contract.

1. `getBanReasons` returns `{StockID, List:[{ZSCode, Reason, Date, SCLT, GNSM, PZSCode, ...}]}`. `ZSCode` contains
   explicit Kaipan plate index codes, `PZSCode` the explicit parent code. These are identities, not names.
2. Only `Type=0` is a limit-up reason. `Type=1` is an anomaly and is excluded. `st=1` is latest-only; `st=200` is the
   full-history request required to recover an earlier target-date reason after a later limit-up overwrites the latest
   result. The captured Type 0 set was measured to match the Kaipan historical pool in the reviewed facts.
3. Reason-to-board attribution is explicit and unique: choose `PZSCode` when present, otherwise the first `ZSCode`.
   Keep all explicit `ZSCode` values for audit. Do not infer a reason or board from membership. A reason with no
   attributable category is marked with `reason_unknown`; it is not assigned to an arbitrary board.
4. Match every explicit board code to `plate_registry`; retain and count unregistered codes without dropping a pool
   member. When dated `plate_members` is available, check the uniquely selected main plate against it; a missing dated
   membership input is unknown, not a mismatch. Membership never infers the reason or selects a different plate.
5. The provider order for the described session is: saved complete `getHisBans` pool row; per-symbol
   `getBanReasons(st=200)`; WenCai's dated `涨停原因类别`; then the latest Type 0 reason with `Date < D` for a D
   premarket conclusion. The last rung is visibly `DEGRADED` / `HISTORICAL_ATTRIBUTION`.
6. `date` is the described pool session, not fetch time. For a premarket run on D, the source date must be strictly
   earlier than D. The builder enforces `event_date <= date`, which is the same boundary when `date = D-1`.
7. Coverage denominator is the number of yesterday-pool members. Unresolved members count as missing in coverage;
   they do not get fabricated reason rows. If every source fails, the dataset is typed `MISSING` and the job emits one
   aggregate failure result. The scheduler emits one terminal aggregate failure alert after retry exhaustion.
8. Fetch as early as the yesterday pool is ready. Preserve per-symbol Kaipan checkpoints across retry attempts;
   never restart already completed symbols. The scheduled reason job depends on `limit_pool_d1`, starts at 00:45,
   retries in five-minute intervals (three attempts), and permits late recovery through 08:50.

## Code check

The C581 implementation follows these rules in `limit_reason_theme.attribute_main_reason`,
`data_pipeline.limit_reasons.build_limit_reasons`, and `KaipanProvider.limit_reasons`. The previous CLI adapter called
the SDK's default `getBanReasons` method, whose default was `st=1`; C581 now routes it through the sized SDK adapter
and the provider requests `st=200`. The provider filters Type 0 and dates after the described session, writes a
per-symbol checkpoint, and keeps unregistered codes while reporting identity mismatches. The dataset primary key is
`(date, symbol)`; it is distinct from the raw provider history key `(symbol, event_date, type)`.

## Contract and TD mapping

The executable contract is `src/engine_core/data_pipeline/dataset_specs/limit_reasons.json`. It declares one row per
resolved pool member, `(date, symbol)` as the primary key, `pool_members` as the coverage basis, and `event_date <=
date` as the temporal boundary. Both Kaipan rungs are formal primary providers; the later WenCai and historical
attribution rungs are degraded. The timezone-aware point-in-time cutoff is included in version identity so a corrected
cutoff creates a new immutable version. Reason records are cold data; the TD target is `core_data.limit_reasons_v1`.

| TD field | Type | Catalog field / rule |
| --- | --- | --- |
| `ts` | `TIMESTAMP` | `date` at 00:00 Asia/Shanghai |
| `symbol` tag | `VARCHAR(8)` | six-digit `symbol` |
| `event_date` | `VARCHAR(10)` | selected reason date; null when unavailable |
| `reason` | `VARCHAR(2000)` | selected source text |
| `main_reason` | `VARCHAR(256)` | single attributed reason category |
| `reason_unknown` | `BOOL` | explicit unknown marker |
| `plate_code` | `VARCHAR(16)` | unique main plate (`PZSCode`, else first `ZSCode`) |
| `plate_codes` | `VARCHAR(256)` | JSON array of source-provided board codes: all `ZSCode` values from detailed API rows, or the saved pool row's single code |
| `parent_plate_code` | `VARCHAR(16)` | explicit `PZSCode` |
| `plate_registry_match`, `plate_member_match` | `BOOL` | tri-state identity checks; null means source unavailable |
| `plate_identity_status` | `VARCHAR(32)` | registered/member/mismatch/unverified result |
| `source` | `VARCHAR(96)` | selected source rung |
| `status` | `VARCHAR(32)` | `READY` or `DEGRADED` |
| `version` | `VARCHAR(24)` | immutable catalog version |

TD schema creation or live TD writes were not part of the C581 validation run; the mapping above is the reviewed
target contract, not evidence that a TD table was created.
