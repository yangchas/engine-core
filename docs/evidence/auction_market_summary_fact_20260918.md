# A2 market summary fact wheel — 2026-09-18

## Source evidence

The fixture `tests/fixtures/facts/auction_market_summary_20260914.json` is a
small frozen extraction of the real `auction_0920.json` capture's Redis
`summary` field. It preserves the observed values and does not contain a
synthetic market row set.

The new pure wheel is:

```text
normalize_auction_market_summary(raw, trade_date, source_id, ...)
    -> AuctionMarketSummaryFact
```

It accepts legacy raw aliases only at the boundary and exposes canonical fields
with explicit count/yuan units. Missing fields remain `None`; explicit numeric
zero remains zero; malformed or negative values become `INVALID`, never zero.
Source identity, source table, observation time and evidence references belong
to `evidence_hash`, not `content_hash`.

## Verification

- local targeted tests: `6 passed`
- local full suite: `480 passed`
- cobra-ion isolated full suite: `480 passed`
- local/cobra fixture SHA-256:
  `65171b1894f8dc6cc08e4ff7c573bf0db972a1a2e814566a5cb25d26fd985a7f`
- compileall: PASS on both environments
- no production service, Redis, TDengine, RabbitMQ, notification, or effect was touched

## Parity boundary

This closes only the explicit A2 summary normalization contract. It does not
claim full legacy email parity, plate aggregation parity, or report delivery
ownership. The source `ts` is carried only as supplied observation evidence;
it is not reclassified as Rabbit arrival time or exchange tick time.
