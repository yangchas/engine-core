# Legacy theme-weight wheel audit (2026-09-18)

## Scope

This change extracts only the pure snapshot-to-theme contribution step from
`engine_next/strategy_skill_layer/auction_plate_buckets.py::_resolve_theme_weights`.
It does not migrate theme providers, Redis mappings, hot-plate ranking, signal
labels, strategy thresholds, reports, or effects.

## Contract

`resolve_legacy_theme_weights(plate, real_plate_names)` returns at most two
ordered `(theme_id, weight)` pairs:

1. the first valid token from `plate` is the primary candidate;
2. additional candidates come from `real_plate_names` in input order;
3. a generic primary is moved behind a non-generic secondary;
4. primary/secondary weights are `1.0`/`0.6`;
5. generic names are multiplied by `0.18`.

The result is a pure input transform. Missing names stay absent. It does not
interpret a weight as capital flow and does not perform I/O.

## Legacy parity

| capability | legacy location | new wheel | status |
|---|---|---|---|
| split/normalize theme tokens | `engine_next/runtime/plate_mapping_registry.py` | `split_theme_tokens` / `normalize_theme_name` | MATCH for covered rules |
| generic theme detection | same registry | `is_generic_theme` | MATCH for covered names/keywords |
| primary/secondary ordering | `_resolve_theme_weights` | `resolve_legacy_theme_weights` | MATCH |
| two-level weights and generic discount | `_resolve_theme_weights` | constants + pure function | MATCH |
| Redis theme mapping authority | runtime providers | not implemented | UNKNOWN / not migrated |
| hot-plate ranking and strategy labels | controller/strategy layer | not implemented | NOT_APPLICABLE to this wheel |

The local tests are a differential contract for the extracted behavior. No
claim is made that Redis `config:plate_mapping:s2p` is historically available
or authoritative for replay; the real Redis probe remains dialect/coverage
evidence only.
