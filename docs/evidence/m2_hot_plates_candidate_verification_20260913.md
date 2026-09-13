# M2 hot-plates candidate verification — 2026-09-13

## Scope

Read-only verification of the current Cobra release and the Core candidate
contract. No candidate writer was deployed; no Redis/TD write, Rabbit ACK,
repair, notification, or strategy effect was executed.

## Candidate identity

```text
spec: docs/evidence/m2_hot_plates_meta_recipe_v1.md
spec_sha256: 9d95a64c2de9ae86f9475f0f0dec2a80164b6adbf5429ad9716fd61f2b2ec5fd
transform_id: engine_next.hot_plates_meta.v1
allowed_path: engine_next/runtime/intraday_data_hub.py
```

The candidate is a metadata-only extension of the existing hot-plates writer:
explicit `HotPlatesV1`, `available_at_ms`, field units, canonical payload hash,
row count, trade date, and source. The existing Kaipan request and Redis hash
layout remain unchanged.

## Cobra read-only observation

Release:

```text
commit: e272842c8f490f55a1b017badb71e71904ce008e
path:   /home/exedev/services/engine-next/releases/20260903_e272842
```

On `cache:hot_plates_meta:2026-09-10`, the current metadata contained date,
source, row counts, success, and `updated_at*`/`last_attempt_at*`, but no
`schema_version`, `available_at_ms`, `field_units`, or payload hash. The
companion `cache:hot_plates:2026-09-10` was a Redis hash with 50 rows. The
reader therefore remains `OBSERVED/UNAVAILABLE` with
`available_at_unknown`, as required by the Core contract.

## Local verification

```text
python -m pytest -q
271 passed
python -m compileall -q src tests examples
PASS
git diff --check
PASS
```

## Decision

```text
HOT_PLATES_CANDIDATE_SPEC       PASS
CURRENT_PRODUCTION_METADATA    BLOCKED
M2_AUTHORITY_CLOSURE            BLOCKED
```

The next permitted action is an isolated candidate transform verification
against the exact production source, followed by independent audit and a
normal-run Cobra shadow. Until those steps produce the explicit metadata in
production, hot plates must not enter Core runtime.
