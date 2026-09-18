# Real Redis cache inventory — 2026-09-18

## Scope

This inventory used the deployed Redis instance on `cobra-ion` and only
`TYPE`, `HLEN`, `HSCAN`, `GET`, and `STRLEN` reads. It did not call repair,
writer, fallback, Rabbit, TDengine, notification, or effect code.

Artifact: `cache-inventory-20260918.json`  
SHA-256:
`06ed5c63b66c854f94008f9dc1d048a7038d4ba63fa9f15032d60f5c06c69837`.

Requested trade date: `2026-09-18`; previous trade date: `2026-09-17`.

## Dated cache observations

| Key | Rows / state | Date check | Metadata issue |
|---|---:|---|---|
| `cache:yest_limit_pool:2026-09-17` | 47 | all payload rows dated 2026-09-17 | no schema version, units, or verified availability |
| `cache:hot_plates:2026-09-18` | 50 | all payload rows dated 2026-09-18 | no schema version, units, or verified availability |
| `cache:hot_rank:2026-09-18` | 100 | all payload rows dated 2026-09-18 | no schema version, units, or verified availability |
| `cache:limit_truth:2026-09-18` | missing | unavailable | no key/meta |
| `cache:stock_extra:2026-09-18` | missing | unavailable | no key |
| `cache:chip_peaks:2026-09-18` | missing | unavailable | no key |

All present hash scans were internally consistent (`HLEN` before/after scan
matched the item count). Their payloads are real and date-labelled, but the
missing availability/schema/unit metadata means Core must not promote them to
historical replay inputs without an explicit contract.

## Static mappings and dialect findings

- `config:plate_mapping:s2p` exists as a 2914-field hash whose values are JSON
  lists; both `config:plate_mapping:info` and
  `config:plate_mapping:full_sync_info` are absent.
- `market:stock_plate` exists as a 5955-field hash, and
  `market:stock_reason` as a 2486-field hash. Their values are not JSON under
  the current inventory decoder, so they require a producer/consumer dialect
  audit rather than being declared corrupt.

## Migration decision

The real Redis source/action inventory is now concrete for the current trading
day. The next Core work should consume only the dated fields with an explicit
status/provenance contract. Missing keys and missing metadata remain
`MISSING`/`UNAVAILABLE`; no cache repair or semantic fallback is authorized by
this observation.
