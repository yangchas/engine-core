# Auction Shadow theme-delta Engine integration

Date: 2026-09-18  
Branch: `codex/feature-session-engine-integration`

## Scope

This change wires the already differential-tested legacy theme-delta rule into
the existing `AuctionShadowStrategy` trace. It is an opt-in composition path:

```text
FrozenDataBundle
  └─ DataResult(function_id=theme_auction_delta_compat)
       └─ data["facts"]
            └─ build_legacy_theme_delta_shadow_trace()
                 └─ FACT_ONLY trace
```

The integration does not add a provider, scheduler, workflow, effect, or
network access. The default `AuctionShadowStrategy` behavior is unchanged when
`theme_delta_function_id` is omitted.

## Contract

- The function identifier is `theme_auction_delta_compat`.
- Theme facts are accepted only as ordered mappings from a frozen
  `DataResult.data["facts"]` value.
- Numeric fields are validated as finite values.
- The signal is recomputed from numeric fields; a supplied signal is accepted
  only when it matches the legacy precedence and thresholds.
- Facts are sorted by `theme_id` before semantic and evidence hashing, so input
  completion order does not change the trace identity.
- Evidence references are normalized, deduplicated, and promoted to the
  top-level `StrategyResult.evidence_refs`.
- The output remains `FACT_ONLY`; no BUY/PASS/EV/effect decision is emitted.

## Verification

Local verification on the final implementation:

```text
python -m pytest -q -p no:cacheprovider
570 passed

python -m compileall -q src tests examples
PASS
```

The existing real Redis differential evidence remains the source-of-truth for
the compatibility rule. This Engine integration is a pure composition check;
it does not claim that the full production strategy or `engine-next` has been
replaced.

## Safety boundary

This path is read-only and fact-only. It does not consume RabbitMQ, write
Redis/TDengine, send notifications, or trigger effects. The next verification
step is a bounded real Redis Engine shadow that supplies the same frozen
`DataResult` shape; only after that evidence is complete should the report
projection be considered for migration.
