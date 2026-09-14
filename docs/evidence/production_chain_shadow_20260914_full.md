# Production-chain shadow (captured evidence) — 2026-09-14

## Scope and identity

- Executable Core commit: `cb5d6fd`
- Cobra archive: `/home/exedev/validation/engine-core-cb5d6fd.tar`
- Archive SHA-256: `1019bb54fbdcdf87a2374fe639bff96067ed334249fae0a25474ec37c46460a7`
- Formal runtime: `/home/exedev/services/engine-next/shared/venv/bin/python` — Python 3.12.3
- Remote capture: `/home/exedev/audit/production_ground_truth/20260914`
- Remote output: `/home/exedev/validation/production-chain-shadow-20260914-full`
- Run mode: read-only captured-file audit; no Redis/TD connection was opened by the audit tool itself.

The audit consumed the real capture directory plus two separately captured, bounded
TD artifacts from the same trading day:

```text
/home/exedev/validation/engine-core-live-20260914/auction_shadow_600519_20260914.json
/home/exedev/validation/engine-core-live-20260914/td_stock_tick_092450_093001.jsonl
```

The TD artifacts are evidence inputs, not proof that they share a Rabbit batch or
internal AuctionState with the Redis capture. That relationship remains UNKNOWN.

## Safety boundary

```text
new Rabbit consumer       0
Rabbit ACK/publish        0
Redis write               0
TD write                  0
notification/effect       0
production restart       0
```

`engine-next` and `t1-v2-live` remained the production owners.

## Acceptance result

```text
SOURCE_INGESTION_ACCEPTANCE        UNKNOWN
AUCTION_STATE_ACCEPTANCE           OBSERVED
STORAGE_PROJECTION_ACCEPTANCE      WARN
ENGINE_NEXT_CONSUMPTION_ACCEPTANCE UNKNOWN
ENGINE_CORE_SHADOW_ACCEPTANCE      PARTIAL
JOINT_TRADING_DAY_ACCEPTANCE       WARN
```

The result is not a production failure. It records the observable boundary:

- `auction_0920` and `auction_0925` Redis projections were captured; the required
  `auction_0924` slot was empty at capture time and remains `MISSING` in this
  evidence. No 0924 artifact was synthesized from later observations.
- Gateway/Rabbit batch membership, `emit_a25`, and internal AuctionState/freeze
  ordering were not present in the capture, so they remain `UNKNOWN`.
- No engine-next read-only loader trace was captured, so the final report path is
  not claimed equivalent.

## Real Q2 shadow

The latest captured file `q2_093210.jsonl` contained 5,220 unique symbols and
5,220 normalized quotes:

```text
coverage                 1.0
status                   STALE
consistency              BEST_EFFORT_STALE
stale symbols            5220
source time range        2026-09-14 00:00:00+08:00 → 09:31:14+08:00
repeat Core probe hash   equal
```

Coverage therefore remains distinct from freshness/completeness. The Q2 adapter
preserved `source_record_time_ms` and did not reinterpret it as Rabbit arrival or
exchange tick ordering.

## Real auction and TD evidence

The capture contained projection-only Redis snapshots:

```text
0920: 4914 valid rows, top_amount_count=200
0924: MISSING (capture status FAILED: empty Redis key)
0925: 5207 valid rows, top_amount_count=200, anchor present
```

The bounded TD `auction_snapshot_v2` source-row artifact for 600519 contained
three source rows (0920/0924/0925). Core recomputed the fact from those rows,
without accepting a precomputed shadow result:

```text
status          OBSERVED
shadow_status   PARTIAL
decision        FACT_ONLY
state           OBSERVE
segment_count   2
content_hash    0df98bbaa5072f03309075a1c9e3484f115aaeef60d5300bb2afa7b6e29f9aa6
evidence_hash   53205bf3528b8e97b07864bef75fc7d92a4340256a586a625de549eb7caed5af
comparison_hash 6ed1d5c928d88e2b1462b96b6a621af9224a4a55fd37b03a2fc6642cd1a3451e
```

This is source-formula evidence only. It does not establish Redis/TD writer
projection equality or Rabbit/runtime causality.

The TD tick sample had 11 normalized rows in the 09:24:50–09:30:01 interval.
All rows were intentionally left `UNCLASSIFIED`; morphology was recorded without
inventing an auction/post-auction predicate. Same-symbol/same-timestamp order is
`UNKNOWN` because no source ordering key was present.

## Output artifacts and hashes

```text
audit_summary.json          446c27cad04e1c8dec864a9018cddd7e101d83d6a5910ff5557fddf7aea58ca6
production_chain_matrix.csv f56144e42d36a3c6ba0457c05048a3ea8433038a710c6a412ade6cc775c18c25
tick_shape_samples.jsonl    69bcec164573f6bea8ba3c32cac1b7709784a2baeae2753bee510d7ae28fd4ee
tick_shape_transition.csv   f35d513009d69bab2d3ec2de9526909a1d75120c2c1e6aa74408a7a30093a2f2
tick_shape_statistics.csv   8871b8b85540db9375f3e05cf21aa3182130b29098bf5f8db6c53b0b91782a54
tick_shape_audit.md         21b417b9fcba2943873ed5e1a35d25ab8f54d5f8a30b8cc90338e2bf90409181
```

The audit tool only reads captured files, normalizes them, runs the in-memory Core
Q2 path, and writes a new evidence directory. It does not repair missing slots,
connect to production services, or send effects.

The `cb5d6fd` audit fix keeps the independent TD source-row shadow out of a
missing or aggregate capture row: `auction_0924/engine_core` and
`auction_anchor/engine_core` are `UNPROVEN`, while the separate
`auction_fact_shadow` remains `OBSERVED`. This prevents a later TD artifact from
being mistaken for production-chain capture evidence.

## Next action

Do not add another production connector or change the producer from this evidence.
The next authoritative observation is a normal in-session run started before
09:15, followed by a separate engine-next loader trace if that trace can be made
read-only. Until then the correct status is `WARN/PARTIAL`, not replacement-ready.
