# Real Redis Q2 opening fact probe — 2026-09-10 13:13 CST

## Result

> Historical note: this run predates commit `130ba28`, which corrected the
> probe's `semantic_hash` field to use the versioned `SemanticHashV1` contract.
> Use `real_opening_facts_20260910_1320.md` for the current hash contract.

The latest `engine_core` commit was executed on `cobra-ion` with the shared
Python 3.12 runtime against the production Redis Q2 projection.  The probe
was read-only and completed successfully for the bounded symbols
`000001`, `300750`, and `600519`.

| Check | Result |
| --- | --- |
| projection status | `READY` |
| quote count | `5219` |
| expected symbol count | `5219` |
| coverage | `1.0` |
| freshness | `FRESH` |
| stale symbols | `[]` |
| projection consistency | `BEST_EFFORT` |
| read-only boundary | `SMEMBERS/HGETALL` only |
| Redis/TD/Rabbit writes | `0` |
| notifications/effects | `0` |

The source timestamp range reported by the projection was:

```text
oldest_source_time_ms = 1788969600000
newest_source_time_ms = 1789017065000
```

The three opening facts were built by the pure `build_open_fact` wheel.  The
probe output retained independent `status` and `limit_state_status`; no
limit state was inferred from price change.  The full JSON output is kept at
`tmp/core-real-opening-136cedd.json` outside the repository evidence path.

## Identity

```text
core_commit: 136cedd
remote_archive_sha256: 2F3B033685EDC98B3AD67FD7B8A4D2B0008E13AA7816C4411700C73F3C4CD459
probe_output_sha256: AA6699CBA80F126AA15306621DCDF6BE8B4C48F2E9D93E64504816E33BF613CF
semantic_hash: a80601f5d51a183451db5ae33cd4cf1e5e64be14ddb76febb7b96eda88d774f8
projection_hash: 5fee22229a1aa05513e78379e7c33928e0751a27776e1c51698b2b95df2fe797
```

## Boundary

This closes only the real Redis-to-opening-facts read path.  It does not
claim that Redis Q2 is an immutable historical snapshot, that `ts` is an
exchange tick timestamp, or that the full auction/opening strategy and
reporting chain has migrated.  No production service was restarted or
modified.
