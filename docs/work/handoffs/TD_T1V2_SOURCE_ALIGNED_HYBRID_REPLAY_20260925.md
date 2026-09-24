# t1-v2 release-calculation/current-reader hybrid replay — 2026-09-25

Status: `Q2_PARITY_PASS / AUCTION_BARRIER_PARTIAL / PHASE_P_PARTIAL`

## Goal and boundary

Test whether the calculation-source differences identified in
`TD_T1V2_SOURCE_ALIGNMENT_AUDIT_20260925.md` explain the observed Q2 mismatch,
without changing the deployed release or current development worktree.

The validation build used the deployed release calculation files plus the
current 3-second TD replay reader and slice barrier. It was staged under
`/home/exedev/validation/t1v2-source-align-20260925T033757+0800/`; source and
binary artifacts are outside the production release tree. The current TD
reader/barrier overlay was limited to replay code. Validation-only API
compatibility changes were confined to the staging copy.

Real TD input: `market_data1.stock_tick_v2`, trade date `2026-09-23`, window
`[09:15:00,09:25:09)`, read one 3-second half-open slice at a time.

Redis output: DB15, unique prefix
`task009p_cross_20260925T033757:`. TD writes were explicitly disabled;
Rabbit environment was unset and no Rabbit consumer/ACK path was used. No
production directory was modified and no service was restarted.

## Build provenance

- Deployed release source snapshot:
  `/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/content/source/C`.
- Release manifest verification succeeded. The release metadata names base
  commit `9fd4a42b3f3944235da89e1ae2278ea93cff193c`; that object is not present
  in the local t1-v2 Git object database, which has no remote configured.
- Source-pinned full build binary:
  `.../t1_v2_release_source`, SHA-256
  `373f7c64386e6b4ab24a632ada6122f74979fe91dce258f90e992513ad03c790`.
- Hybrid replay binary:
  `.../hybrid-current-reader/t1_v2_release_core_current_reader`, SHA-256
  `424c4f57b8c1b0a1948602ac6906e7720ee8039484343a4a808b4f18d33a6d48`.
- The pure source build did not byte-match the deployed binary SHA
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.
  Therefore this is source-level alignment evidence, not binary-identical
  reproduction.

## Redis readback comparison

Baseline: DB5 / `task009k:`. Hybrid output: DB15 /
`task009p_cross_20260925T033757:`. Comparison read back Redis hashes only; no
market values or credentials were included in this report.

| Output | Readback result |
|---|---|
| Q2 per-symbol hashes | 5,222 vs 5,222; all keys and hash fields/values equal |
| Q2 active-symbol set | 5,222 members on both sides; equal |
| A2 0920 | `meta.n=4,872` vs `4,873`; `top_amt`, `top_br`, `top_chg` differ |
| A2 0924 | `meta.n=5,099` vs `5,100`; `top_amt`, `top_br`, `top_chg` differ |
| A2 0925 | exact hash equality; `meta.n=5,208` on both sides |
| Legacy auction 0920/0924 | summary and `top_amount` differ |
| Legacy auction 0925 | exact hash equality |
| 0925 anchor | exact byte equality; 539,474 bytes |
| `latest` metadata | run timestamp differs; not treated as semantic parity |

Comparison checksum algorithm: SHA-256 over sorted Redis key suffixes and
length-prefixed sorted hash fields/values. This is an audit checksum, not a
Core canonical hash.

```text
Q2 baseline and hybrid: 308a928ebdee7b75c2ef762c9b518bb97a358932553a350f53affbef5b9ff04f
A2 hybrid:              8f3d3fab5b648f59bc3f41a8bf93b563885e6ae857487a9a92b34eb5f7aeb3f7
A2 baseline:            93c3207824ecf039d97aef609f86d3dcfe524b41975df09daeab90e502447225
Legacy hybrid:          cdb3c1fbea56e5ace8b4fc1cf22e5c819dc4383cef61590fdc9262a2a344c9d4
Legacy baseline:        e6e1ebd23d345e277f394e71a8ae1df2832e1d5bc64ebe22afeb3517789e4a45
0925 anchor, both:      1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb
```

The DB15 namespace contains 5,233 keys: 5,222 Q2 symbol hashes, four A2
hashes, four legacy auction hashes, one active-symbol set, one anchor string,
and one runtime hash. The namespace is intentionally retained as validation
evidence; this report does not authorize its deletion.

## Interpretation

The result narrows the earlier source mismatch:

- Release calculation files plus the current reader/barrier reproduce the
  complete Q2 Redis projection against the DB5 baseline for this date/window.
- The final 0925 auction projection and anchor also reproduce exactly.
- The 0920 and 0924 barrier snapshots remain different by one counted member,
  with ranked/summary fields changed. This is not hidden by final Q2 parity.
- An earlier validation barrier binary matched 0920/0924 as well, but its
  reader/scheduler differed and its evidence records 204 batches; this hybrid
  run ended with runtime `seq=207`. The remaining delta may be barrier-time
  event membership/timing or reader scheduling. Root cause is `UNKNOWN`.
- No repeat of this exact hybrid binary/output was performed, so this report
  does not claim hybrid-run determinism.

```text
RELEASE_CALCULATION_Q2_PARITY=PASS
AUCTION_0925_PARITY=PASS
AUCTION_0920_0924_PARITY=PARTIAL
PHASE_P=PARTIAL
RABBIT_LIVE_MEMBERSHIP_OR_ARRIVAL=UNKNOWN
HISTORICAL_AVAILABLE_AT=UNKNOWN
M3_1_NORMAL=BLOCKED
TD_WRITE_HEALTH=UNPROVEN
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

Post-run service inspection: `t1-v2-live=active`, PID `304`, `NRestarts=0`;
`engine-next=active`, PID `281775`, `NRestarts=0`. Root filesystem had 22 GB
available. No systemd action was taken.

## Next bounded action

Add replay-only diagnostics for source-symbol membership at the 09:20 and
09:24 business barriers. Use the same real date and 3-second TD slice reads,
keep TD read-only and Redis writes in a fresh isolated non-DB0 namespace, then
compare the exact member set and barrier position with the earlier
source-aligned barrier evidence. Do not synthesize a missing member or infer
Rabbit arrival order from TD event time.

## Inclusive barrier follow-up — 2026-09-25 04:49

The one-member 0920/0924 deltas described above were tested against a concrete
boundary hypothesis: because the business time is truncated to a second, TD
rows in that same second must be applied before the snapshot. The replay barrier
was changed to defer an anchor at the half-open slice's right edge and to let a
real tick in the anchor second trigger the snapshot. The validation copy also
uses an explicit Clock control event only when that second has no row.

After the change, the source-aligned validation binary
`/home/exedev/validation/t1v2-barrier-second-inclusive-20260925T041338+0800/t1_v2_inclusive_final`
(SHA-256 `0cf2c5f7f1cede0770d592d5e9c3b275ebc346385139f1cbc6cc26ff2fb83208`)
was replayed on the same real window. Re-reading isolated Redis DB15
`task009pbarrierinc20260925T042334:` against DB5/`task009k:` confirmed exact
equality for all 5222 Q2 hashes, all three A2 and legacy auction snapshots,
latest metadata, and the 0925 anchor. The prior one-member count deltas are
therefore resolved in this source-aligned experiment.

The patch is also ported to the current development source and committed as
`acf277bbba0d3bab90aa6550a23850e2c8aa7013`. The current development-source real
replay aligns `meta.n` and trigger timestamps for all three anchors, but still
differs in Q2 fields and auction ranked/summary content because that branch is
not calculation-source equivalent to the deployed release. It does not prove
exact current-dev member-set parity. This updates the earlier `Next bounded
action`: do not continue tuning the barrier; next, audit calculator/source
semantics under a fixed source before proposing a separate implementation.
Rabbit membership/arrival and historical `available_at` remain UNKNOWN, and
Phase P/M3-1 remain PARTIAL/BLOCKED.
