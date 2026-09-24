# t1-v2 replay source alignment audit — 2026-09-25

Status: `SOURCE_ALIGNMENT_BLOCKED`  
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
DEVELOPMENT_BUILD_REPEATABILITY       PASS (prior real replay evidence)
DEVELOPMENT_VS_DEPLOYED_SOURCE_PARITY  NOT_COMPARABLE_YET
PHASE_P                               PARTIAL
M3_1_NORMAL                           BLOCKED
TD_WRITE_HEALTH                       UNPROVEN
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

## Next bounded action — remain in Phase P

Before another batching-sensitivity run, produce a replay-only validation build
whose calculation files are pinned to the deployed release source snapshot.
Keep the release directory untouched; stage the build under a dedicated
`/home/exedev/validation/` directory and overlay only the already-reviewed
replay slice/barrier mechanism needed for the same `[09:15:00,09:25:09)` input.
Then:

1. Record source-manifest hashes, build flags/tool versions and resulting
   validation-binary hash.
2. Read the same real TD date/window, one 3-second half-open slice at a time;
   preserve all rows and empty frames.
3. Write only to a fresh isolated non-DB0 Redis namespace; keep TD writes and
   Rabbit consume/ACK disabled.
4. Compare Q2 and 0920/0924/0925 outputs with the existing DB5 baseline and the
   `e91a20a` result, field by field. Repeat once to verify deterministic output.
5. Classify the remaining delta as source-semantic, barrier/batch, order, or
   still `UNKNOWN`; do not force a PASS by filtering or changing market facts.

Do not copy the deployed source into the development branch wholesale, do not
modify the live release, and do not promote Phase P or start a new migration
phase from this audit. If a source-aligned staging build cannot be made
reproducible, record that as the next blocker and keep the already-valid
single-build repeatability evidence separate.
