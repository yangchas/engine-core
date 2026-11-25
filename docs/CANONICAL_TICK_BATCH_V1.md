# Canonical Tick/Batch V1

This document is the Core-side contract for parsed Rabbit `DataBatch` values
and already-read TD rows.  The authoritative shape is the existing RabbitMQ
`DataRecord/DataBatch → RawTick/TickBatch` path:

```text
canonical source authority = RABBITMQ_DATASERVICE_RAWTICK_V1
```

TD is a compatibility source only.  It may populate fields present in the
RawTick contract, but it may not add TD-only fields or redefine Rabbit field
units/semantics.  This is not a Rabbit consumer contract and does not grant
Core permission to read or write production systems.

## Boundary

```text
parsed Rabbit DataBatch ─┐
                         ├─ SourceAdapter → MarketTickV1/TickBatchV1
already-read TD rows ────┘
```

Adapters are pure in-memory functions.  The canonical package must not import
Rabbit, Redis, TDengine or production service clients.

## Identity and quality

`MarketTickV1` follows the existing C++ `RawTick` field set.  Amount and
volume snapshots are cumulative; `inst_vol`, `inst_amt_yuan` and
`large_net_yuan` are event-level increments/derived increments.  Prices use
milli-price, amounts use yuan and volumes use the existing C++ `vol_units`
unit.

Every public field has `FieldMetaV1(quality, provenance, invalid_reason)`.
The legal quality/value combinations are:

```text
PRESENT_VALUE            non-null, finite and in range
MISSING                  typed NULL; source absence is proven
UNKNOWN                  typed NULL; presence is not proven
WIRE_DEFAULT_AMBIGUOUS   wire default value retained; proto3 presence unknown
INVALID                  typed NULL; invalid_reason is required
```

`Missing != Zero`.  NaN, Infinity and overflow never enter a canonical value.
Proto3 default-valued scalars without presence are ambiguous per field, not
for the whole tick.

`trade_date` is strict `YYYY-MM-DD`; every tick in a batch must have exactly
the batch trade date.  The `market_code` value is the source-defined C++
`RawTick.market` code.  No new exchange/board taxonomy is introduced here.

Symbol normalization reproduces the existing C++ paths: Rabbit source values
must satisfy `RawTickConverter::copy_symbol` (six ASCII digits); TD values use
the existing `TdReplayRowConverter` prefix stripping and then the same six
digit check.

## Hashes

All hashes use SHA-256 and `CanonicalHashEncodingV1`: UTF-8, fixed contract
field order, fixed-width integers, explicit typed NULL tokens, fixed array
indexes, length-prefixed strings, stable symbolic enums, and no unordered-map
or default JSON serialization.

Tick parity is the default comparison scope:

```text
canonical_value_hash
    normalized values, units, schema and typed NULLs

canonical_semantic_hash
    value hash inputs plus field quality/provenance/invalid reason

canonical_content_hash := canonical_semantic_hash
```

Batch identity is separate because Rabbit and TD boundaries are different:

```text
batch_content_hash = H(schema, encoding, ordered tick semantic hashes)

source_evidence_hash = H(
  batch_content_hash, mode, trade_date, logical_ts_ms,
  source, source_batch_id,
  source_sequence, source_sequence_status,
  arrival_order_status, replay_order_status,
  historical_available_at_ms, historical_available_at_status,
  same_event_order_ambiguity, batch_quality
)
```

`seq_no`, `wall_ts_ms` and `run_id` are excluded from source evidence.  They
are included only in:

```text
run_evidence_hash = H(source_evidence_hash, seq_no, wall_ts_ms, run_id)
```

Parity precedence is:

```text
NOT_COMPARABLE
VALUE_MISMATCH
ORDER_AMBIGUOUS
VALUE_EQUAL_QUALITY_DIFFERENT
STRICT_EQUAL / STRICT_COMPATIBLE
```

`VALUE_MISMATCH` always wins over order ambiguity.

## TD frame and source identity

TD adapters consume one half-open frame `[start_ms, end_ms)` at a time and set
`logical_ts_ms=end_ms`.  Empty frames are retained.  Session-local `seq_no`
is not source identity.  TD source identity is:

```text
td-replay:{trade_date}:{frame_start_ms}:{frame_end_ms}
```

Deterministic replay ordering is:

```text
event_time_ms → normalized_symbol → stable_td_row_key → canonical_semantic_hash
```

If same-time, same-symbol rows have different content and no stable source
key, set `same_event_order_ambiguity=true`, keep arrival order `UNKNOWN`, keep
replay order `SYNTHETIC_DETERMINISTIC`, and do not claim `COMPLETE` or arrival
order parity.

Batch completeness is explicit:

```text
EMPTY     query succeeded and returned zero rows
COMPLETE  source completeness is proven
PARTIAL   known truncation/drop/decode incompleteness
UNKNOWN   data exists but completeness is unproven
```

## Compatibility boundary

The old TD parser and `TDEventV1` replay remain the oracle.  The new adapter
projects in parallel and reports `STRICT_COMPATIBLE`, `ORDER_AMBIGUOUS` or
`VALUE_MISMATCH`.  TASK-006 does not replace the old path or connect any
production source.
