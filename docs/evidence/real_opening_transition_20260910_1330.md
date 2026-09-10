# Real auction-to-opening transition fact — 2026-09-10 13:30 CST

This bounded read-only probe used the deployed TD projections for `600519`
on trade date `2026-09-10`:

```text
auction source: market_data1.a2_20260910_0925_600519
opening source: market_data1.t2_s_600519
```

The source rows were:

```text
auction  ts=2026-09-10 09:25:06.078  px_milli=1291000  pc_milli=1290880  chg_bp=0
opening  ts=2026-09-10 09:30:00       px_milli=1292000  pc_milli=1290880
```

Using the `engine_core` pure wheels:

```text
opening_change_pct = 0.08676251859196515 percentage points
auction_change_pct = 0.0 percentage points
delta_change_bp    = 9
```

The legacy deployed `_change_bp` returned the same `9` basis points for the
same numeric inputs.  The source timestamps were preserved as observed; no
timestamp was rewritten to a business anchor.

## Boundary

This is a single-stock fact comparison only.  It does not infer an auction
strategy conclusion, does not assert that TD rows preserve Rabbit batch
membership, and does not treat `chg_bp` as an arrival-order field.  The probe
performed TD `SELECT` only and did not write Redis/TD, consume or ACK Rabbit,
repair data, send mail, or execute an effect.

```text
core_code_commit: 130ba28
legacy_release: /home/exedev/services/engine-next/releases/20260903_e272842
```
