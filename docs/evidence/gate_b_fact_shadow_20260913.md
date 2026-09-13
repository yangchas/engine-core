# Gate B fact-only shadow — 600519 real Cobra double run

Date: 2026-09-13 (Asia/Shanghai)
Core code under test: `94737ca9c73719f6de859d5af5a5324bb6f3a9b`
Source: `market_data1.auction_snapshot_v2` on cobra-ion
Scope: read-only fact composition; no strategy conclusion

## Execution

The exact core archive was copied to an isolated directory under
`/tmp/engine-core-trash/` and executed twice with the cobra-ion Python 3.12.3
runtime.  Each run issued one bounded `SELECT` for the 600519 `0920`, `0924`
and `0925` rows.  No Redis client, writer, repair path, Rabbit consumer,
SMTP/notifier or claim/effect code was imported or called.

Remote raw artifact SHA-256:

```text
run 1  a16618496116203e1207fb2853698cb53381451e5663dfb63b1c011096786da9
run 2  45e1d6e8b1206b09469ac533570c1923e261a4f8e2bf3f187bee545f886a1bdb
```

The raw artifact hashes differ only because each artifact records a different
wall-clock `observed_at`.  The semantic output and evidence output were
identical across the two runs.

## Stable result

```text
status                 PARTIAL
decision_status        FACT_ONLY
state                  OBSERVE
segment A coverage     READY
segment B coverage     READY
segment A hash         fa78051f6078cf2cafb4df479897847f6c9ba02d94705a54519175ef51e55b96
segment B hash         a29b095bd237561ae4862a6d11d487accfe5321c04617e89ab54b42ab4be5c73
comparison hash        da98e73244205a742d53c5a7f198721567378b67ff90717a8b056ae2fa51a89b
shadow semantic hash   4206e5cdbe6697a463bb48e1d0201b7c5111a7d13c3a6d8529e2e2066f5b83da
shadow evidence hash   d3844fc5eefbccf4d774180d276376ed2d1e67a16645b49bc6dcb43c668248bc
```

The fact metrics were stable:

```text
price_delta_milli       10
amount_delta_yuan       11875800
rest_bid_delta_yuan     -4959000
rest_ask_delta_yuan     130501
pressure_delta_yuan     -5089501
```

`BREADTH_UNAVAILABLE` and `THEME_UNAVAILABLE` remain explicit.  They were not
filled from another source.  The TD timestamp is preserved as
`source_record_time_ms`; it is not treated as Rabbit arrival or batch order.

## Gate conclusion

```text
GATE_B_AUCTION_FACT_SHADOW = PASS
```

This PASS means only that the already verified P/M/RB/RA fact wheels compose
through the read-only engine boundary deterministically on real TD rows.  It
does not mean that an Auction Strategy, M2 reference data, Opening parity,
Rabbit batch equivalence, or production replacement of `engine-next` is ready.
