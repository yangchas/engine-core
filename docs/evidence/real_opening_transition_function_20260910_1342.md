# Real `build_opening_transition_fact` validation — 2026-09-10

The latest core code was executed on `cobra-ion` against the production TD
projections for `600519`.  The input bridge used the 0925 `chg_bp` value
(`0`, converted to `0.0` percentage points at the source boundary) and the
first 09:30 `stock_tick_v2` row (`px_milli=1292000`, `pc_milli=1290880`).

The pure function returned:

```text
auction_change_pct: 0.0
opening_change_pct: 0.08676251859196515
delta_change_pct: 0.08676251859196515
delta_change_bp: 9
delta_state: expanded
sign_state: expanded
status: available
```

The deployed legacy `_change_bp` returned `9` for the same delta.  This is a
fact-only result; no strategy threshold, report, notification, or effect was
executed.

```text
core_commit: cf6c55e
archive_sha256: C22DBF0F76D275DD3F53DB817637C7647E950865B4122B598571861AD6644BE0
source: TD SELECT only
side_effects: none
```
