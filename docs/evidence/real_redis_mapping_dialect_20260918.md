# Real Redis mapping dialect — 2026-09-18

This bounded read-only sample was taken from cobra-ion with the deployed
engine-next Python environment. It used only Redis `PING`, `TYPE`, and bounded
`HSCAN`; no writer, repair, network fallback, Rabbit consumer, or notification
was invoked.

## Observed keys

| Redis key | Type | Value shape | Date/availability metadata |
|---|---|---|---|
| `market:stock_plate` | `hash` | symbol → plain UTF-8 text plate name | none observed |
| `market:stock_reason` | `hash` | symbol → plain UTF-8 reason text | none observed |
| `config:plate_mapping:s2p` | `hash` | symbol → JSON-encoded list of plate strings | no date/availability metadata |

The first two keys are **not JSON payloads**. Treating every hash value as JSON
is therefore an incorrect decoder. The mapping key is JSON at the field-value
level, while the runtime primary plate and reason keys are plain strings.

The terminal displayed Chinese text with encoding replacement characters, but
the sampled byte values were valid UTF-8 payloads (for example the plate value
bytes began with `e7 94 b2 e9 86 87`). The byte-level evidence is retained in
the remote command output; no text repair was applied during this probe.

## Current Core decision

These runtime enrichment keys are not yet promoted to a Core historical
`DataFunction` because they have no explicit trade date or verified
`available_at` metadata. They may be used only through a future bounded live
read-only mapping adapter with status/provenance preserved. They must not be
used to reconstruct a historical replay or silently fill a missing theme.

The old engine-next reader behavior is now explicit:

```text
market:stock_plate       → string hash reader
market:stock_reason       → string hash reader
config:plate_mapping:s2p  → JSON-list hash reader
```

This closes a real schema/dialect ambiguity without changing the production
writer or adding a new provider framework.

