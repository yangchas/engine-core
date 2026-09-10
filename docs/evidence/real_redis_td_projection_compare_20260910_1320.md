# Real Redis/TD auction projection comparison — 2026-09-10 13:20 CST

This is a bounded, read-only cross-source probe for `600519`, executed on
`cobra-ion` with core commit `130ba28`.  It compares only writer-documented
shared fields and aligns Redis metadata to the TD source timestamp; absent
fields are reported as `NOT_COMPARABLE`.

## Observed result

| Anchor | Timestamp alignment | Comparable result | Notes |
| --- | --- | --- | --- |
| `0920` | `MATCH` | `PARTIAL_COMPARABLE` | match amount and rest bid matched; Redis had no rest ask |
| `0924` | `MATCH` | `NOT_COMPARABLE` | no Redis top-row for the bounded symbol |
| `0925` | `MATCH` | `PARTIAL_COMPARABLE` | match amount and rest bid matched; Redis anchor had no rest ask |

Summary: `match=0`, `partial_comparable=2`, `not_comparable=1`,
`mismatch=0`, `timestamp_mismatch=0`.  This is evidence that the available
Redis and TD projections agree where their shared fields exist; it is not a
claim that missing Redis fields equal zero or that every projection is a
complete universe.

## Safety and identity

```text
core_commit: 130ba28
archive_sha256: 1390F1CCB9C2907610B8EEBCAE13460BA2DB344A70765FA5B2AE9154E0E67FC8
probe_output_sha256: ACDCB64D7BC8E63604A9EEDF7E5BC136F4AD1BE440753D885D465A70A03BE17C
semantic_hash: 96842a43a8e85147588f9ef5e9362bc842eb2a2519c605989e0e99271f374434
hash_contract: SemanticHashV1
```

Redis access was `HGETALL/GET`; TD access was `SELECT`.  The probe performed
no Redis/TD writes, Rabbit ACK or consume, repair, notification, or strategy
effect.  The comparison keeps `same symbol + same source timestamp` as the
alignment contract and does not infer ordering within equal-timestamp TD
rows.
