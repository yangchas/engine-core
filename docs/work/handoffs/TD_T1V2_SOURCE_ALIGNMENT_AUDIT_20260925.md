# t1-v2 replay source alignment audit — 2026-09-25

Status: `SOURCE_ALIGNMENT_PARTIAL`
Scope: read-only comparison of the active development source and the deployed
release source snapshot. No service was started/restarted, no production file
was edited, and no Redis/TD/Rabbit access or write was performed in this audit.

## Finding

The 2026-09-23 replay built from development commit `e91a20a` is repeatable
within that build, but it is not a same-calculation-source comparison with the
deployed t1-v2 release. This is a concrete confounder for the 5,206 Q2 hash
differences and the 0920/0924 A2 count differences from DB5. It is not yet a
proved complete root cause; the existing results do not isolate source
semantics from replay batching/barrier behavior.

Keep the acceptance split:

```text
DEVELOPMENT_BUILD_REPEATABILITY                PASS (prior real replay evidence)
RELEASE_CALCULATION_Q2_PARITY                  PASS (controlled real replay)
RELEASE_CALCULATION_AUCTION_BARRIER_PARITY      PARTIAL (0920/0924 differ)
PHASE_P                                        PARTIAL
M3_1_NORMAL                                    BLOCKED
TD_WRITE_HEALTH                                UNPROVEN
```

## Source identity evidence

- Development repo: `/home/exedev/repos/stock-situation-runtime`, branch
  `codex/task-q2-pure-function`, HEAD `e91a20a`; its worktree was clean during
  this audit and it has no Git remote configured.
- Deployed release directory resolves to
  `/home/exedev/services/t1-v2/releases/20260923_tdstop0945b`.
- The release `RELEASE_COMMIT` and `build_info.json` identify base commit
  `9fd4a42b3f3944235da89e1ae2278ea93cff193c`; `build_info.json` describes a
  one-file local 09:45 TD-store cutoff patch and records binary SHA-256
  `363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56`.
- The release source files are covered by `SOURCE_MANIFEST.txt`. The `9fd4a42`
  commit object is absent from the development repo, and there is no remote to
  resolve it against. Therefore a Git-level ancestry/source checkout relation
  between that release and `e91a20a` is not established.
- A bytewise comparison of manifest-listed `C/t1_v2` files against the
  development worktree found 0 identical, 83 different and 2 absent in the
  development tree. This is a broad snapshot difference, not by itself proof
  that every file has semantic changes. The following calculator differences
  were separately inspected and are semantic.
- The current development branch's `quote_calculator.cpp`,
  `auction_calculator.cpp`, and `engine_core.cpp` blobs are byte-identical to
  the `eeb64e6` baseline blobs. Local replay-baseline commits `7a061ce` and
  `b659c3b` are not ancestors of `e91a20a`; they do not make the current build
  source-aligned with the release either.

## Verified semantic differences

1. `AuctionCalculator` matching price: the release uses an effective auction
   price (before 09:25, equal positive level-1 bid/ask; after 09:25, the
   positive `px_milli`). Development uses `px_milli` directly. Before 09:25,
   the release also prices the matched level-1 volumes from that effective
   price; development prices bid and ask independently from their level-1
   prices.
2. Resting auction amount: release uses level-1 price multiplied by level-2
   volume; development uses level-2 price multiplied by level-2 volume. This
   can change `br/ar`, which are among the fields reported as differing in
   most current-development Q2 hashes.
3. Limit-state calculation: release checks the side-specific level-1 auction
   reference price before 09:25; development checks `px_milli` directly.
4. Session/clock behavior: release `EngineCore::on_batch` advances the logical
   clock, handles old-session batches without mutating current state, and
   clears auction state on trade-date advance. The development version sets
   phase/trigger directly in `on_batch` and does not contain those same guards
   in that method.

These differences make calculator/source drift a high-priority hypothesis for
the current Q2/A2 mismatch. They do not prove that all 5,206 Q2 values or the
two anchor count deltas are caused by these formulas. In particular, do not
attribute the remaining difference to batching, source order, or the clock
barrier until those factors are isolated under a fixed calculation source.

## Evidence paths and reproducibility

- Current-source real replay and mismatch:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_CORRECTED_AUDIT_20260925.md`.
- Earlier barrier-aware binary/repeat evidence:
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER_EXPERIMENT_20260925.md` and
  `docs/work/handoffs/TD_RABBIT_PHASE_P_BARRIER23_EXPERIMENT_20260925.md`.
- Release provenance:
  `/home/exedev/services/t1-v2/releases/20260923_tdstop0945b/RELEASE_COMMIT`,
  `SOURCE_MANIFEST.txt`, and `build_info.json`.

The old barrier-aware binary's parity result remains valid for that binary and
its real input. It must not be generalized to the later `e91a20a` build, whose
calculation source differs. Determinism within either build is not cross-build
semantic parity.

## Controlled hybrid replay follow-up — 2026-09-25

A validation-only build then combined the deployed release calculation source
with the current 3-second TD reader/barrier. Its staging source differed from
the release snapshot only in `td_replay_tick_source.cpp/.h` plus the added
`replay_slice_barrier.cpp/.h`; compatibility edits were confined to that
validation staging directory. The binary SHA-256 was
`424c4f57b8c1b0a1948602ac6906e7720ee8039484343a4a808b4f18d33a6d48`.

The binary successfully read the real TD window
`2026-09-23 [09:15:00,09:25:09)` and wrote the run to isolated Redis DB15 under
`task009p_cross_20260925T033757:`. It did not write TD, consume/ACK Rabbit, or
touch a production service. The same output was compared read-only with DB5 /
`task009k:`:

- Q2: 5,222 hash keys on both sides, no key/member or field/value differences;
  active-symbol sets are equal. A length-prefixed digest over sorted Redis
  suffix/field/value tuples is `308a928ebdee7b75c2ef762c9b518bb97a358932553a350f53affbef5b9ff04f`
  on both sides. This is a comparison checksum, not the Core canonical hash.
- A2 0920: `meta.n` 4,872 vs 4,873; `top_amt`, `top_br`, and `top_chg` differ.
- A2 0924: `meta.n` 5,099 vs 5,100; the same ranked fields differ.
- A2 0925: exact Redis hash equality (`meta.n=5,208`).
- Legacy auction 0920/0924 summaries and `top_amount` differ; 0925 matches.
- The 0925 anchor string is byte-identical (539,474 bytes; SHA-256
  `1df35d745018384e6585df125e2c1f78e2df935c2114ed9ff2af20cc320d8bcb`).
- `latest` differs only in run timestamp metadata; this is not a semantic
  snapshot comparison target.

This controlled result supports that the release calculation source restores
Q2 parity for this real input. It does not close the 0920/0924 business-barrier
membership/ranking mismatch. The existing earlier validation binary matched
those snapshots, but used a different replay reader/scheduler and emitted 204
batches rather than this run's 207 frame sequence; therefore the remaining
cause is still `UNKNOWN`, not automatically a data-completeness failure.

The full release-source build SHA-256 is
`373f7c64386e6b4ab24a632ada6122f74979fe91dce258f90e992513ad03c790`; it did
not byte-match the deployed executable, so source provenance is stronger than
binary reproducibility. Both `t1-v2-live` and `engine-next` remained active,
with `NRestarts=0`; root filesystem availability was 22 GB at the post-run
check. Full comparison notes are in
`docs/work/handoffs/TD_T1V2_SOURCE_ALIGNED_HYBRID_REPLAY_20260925.md`.

## Next bounded action — remain in Phase P

Instrument one real replay-only run to capture the exact symbol membership and
source event-time state visible at the 09:20 and 09:24 barriers, then compare
against the old source-aligned barrier evidence. Keep each TD read bounded to
one 3-second half-open slice; do not add symbols, synthesize ticks, or weaken
completeness. Keep writes confined to a fresh isolated non-DB0 Redis namespace;
TD writes and Rabbit consume/ACK remain disabled. Repeat once only if the
instrumented output is deterministic and the first pass identifies a concrete
membership/timing hypothesis. Do not promote Phase P or M3-1 from Q2 parity.
