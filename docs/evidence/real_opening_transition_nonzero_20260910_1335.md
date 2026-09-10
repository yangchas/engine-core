# Non-zero auction/opening delta unit audit — 2026-09-10 13:35 CST

This read-only TD check validates the field-unit bridge used by the first
opening transition fact slice.  For each selected symbol, the 0925
`auction_snapshot_v2.chg_bp` value was compared with the first 09:30
`stock_tick_v2` price/previous-close change.

The current consumer contract is:

```text
auction_change_pct = chg_bp / 100.0
opening_change_pct = (px_milli / pc_milli - 1) * 100
delta_change_bp    = round((opening_change_pct - auction_change_pct) * 100)
```

Representative real rows:

| Symbol | 0925 `chg_bp` | Auction % points | 09:30 open % points | Core delta bp | Legacy delta bp |
| --- | ---: | ---: | ---: | ---: | ---: |
| `688496` | -2054 | -20.54 | -20.547945 | -1 | -1 |
| `000523` | 1009 | 10.09 | 10.091743 | 0 | 0 |
| `600488` | 1007 | 10.07 | 10.074627 | 0 | 0 |
| `600359` | 1004 | 10.04 | 10.045662 | 1 | 1 |
| `002040` | 1002 | 10.02 | 10.020040 | 0 | 0 |
| `000978` | 998 | 9.98 | 9.988649 | 1 | 1 |

All eight queried non-zero samples produced the same integer result from the
new pure `compute_change_delta_bp` wheel and the deployed legacy `_change_bp`.
This supports the current projection/consumer unit mapping for this slice;
it does not claim the upstream vendor's ultimate definition of `chg_bp`, nor
does it establish Rabbit arrival or batch ordering.

```text
core_code_commit: 130ba28
legacy_release: /home/exedev/services/engine-next/releases/20260903_e272842
source: TD SELECT only (market_data1.auction_snapshot_v2, t2_s_* child tables)
side_effects: none
```
