# M2 hot-plates metadata recipe (candidate)

Status: **CANDIDATE / not deployed**  
Date: 2026-09-13  
Production release inspected: `e272842c8f490f55a1b017badb71e71904ce008e`

This is a bounded writer-contract specification for the existing
`IntradayDataHub.fetch_hot_plates()` path.  It is not a new access layer, does
not change the Kaipan request, and must not be applied to the production
checkout until the candidate has passed the trusted verifier, independent
audit, and a Cobra shadow run.

## Recipe identity

```text
transform_id: engine_next.hot_plates_meta.v1
allowed_path: engine_next/runtime/intraday_data_hub.py
```

The recipe may only extend the existing `meta_payload` written beside
`cache:hot_plates:{trade_date}`.  It must not change the provider request,
Redis key shape, row normalization, ranking, or any strategy/freeze behavior.

## Required metadata

For a non-empty response, publish these fields together with the existing
cache metadata:

```json
{
  "schema_version": "HotPlatesV1",
  "trade_date": "YYYY-MM-DD",
  "source": "kaipan",
  "available_at_ms": 0,
  "availability_basis": "writer_observation",
  "field_units": {
    "rank": "ordinal",
    "strength": "UNKNOWN",
    "hot": "UNKNOWN",
    "change_pct": "percent",
    "net_inflow_yi": "UNKNOWN"
  },
  "payload_sha256": "<canonical normalized rows hash>",
  "row_count": 0
}
```

`available_at_ms` is the first successful writer observation for this
payload, not a claim about the upstream provider's publication time.  It may
be consumed for a cutoff only when it is a positive integer and is no later
than the evaluation `knowledge_as_of_ms`.

The payload hash is calculated after the existing connector normalization,
with rows sorted by `(rank, plate_name)` and canonical UTF-8 JSON.  It must
cover the exact rows written to the Redis hash; Redis field iteration order is
not part of the contract.

## Preservation and fail-closed rules

* Empty responses may preserve a prior cache only when its metadata has the
  exact `HotPlatesV1` schema, matching `trade_date` and `source`, a positive
  `available_at_ms`, an explicit `field_units` map, a matching
  `payload_sha256`, and a matching `row_count`.
* Legacy `updated_at*` and `last_attempt_at*` fields never become
  `available_at_ms`.
* Missing, malformed, mismatched, or unverifiable metadata remains
  `OBSERVED/UNAVAILABLE` at the core boundary; no field or unit is inferred.
* The current top-plate scope remains a provider-declared projection.  It is
  not a full-market universe and does not become an authority for theme
  membership or capital flow.

## Verification gates

```text
recipe_hash_matches                         PASS
diff_scope_exact                            PASS
writer_file_compiles                        PASS
schema_units_availability_explicit          PASS
payload_hash_count_date_source_consistent   PASS
legacy_timestamp_not_promoted               PASS
preserved_cache_requires_verified_metadata   PASS
core_runner_fail_closed_on_legacy_meta      PASS
side_effect_boundary_unchanged              PASS
```

The recipe remains **BLOCKED for runtime acceptance** until the production
writer actually emits this metadata on Cobra during a normal run and a
read-only core shadow verifies the same payload hash, row count, date, source,
and units.  No Redis write, TD write, Rabbit ACK, notification, or strategy
effect is authorized by this document.
