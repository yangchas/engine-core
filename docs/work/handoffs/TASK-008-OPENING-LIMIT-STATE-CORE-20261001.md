# TASK-008 opening limit-state cohort summary — 2026-10-01

Status: bounded Core migration slice `PASS_WITH_LIMITS`; overall
`TASK-008=PARTIAL_EVIDENCE` remains unchanged.

## Contract and scope

The deployed `open_confirmation` consumer aggregates `limit_state` separately
from price-based up/down breadth. It reports present/valid/invalid counts,
status, and known up/down counts for opening market/plate summaries. Core
previously preserved this field per symbol but had no cross-sectional summary.

Core now exposes `OpeningLimitStateSummaryV1` through
`build_opening_limit_state_summary()` and includes an observed-cohort summary
in the 09:32 Q2Frame shadow report. The denominator matches the deployed open
comparison: observed per-symbol opening facts with valid price/previous-close
(`status=available`). Missing and invalid limit states remain distinct. Known
`Up/Normal/Down` enum counts are reported with their valid-value denominator,
including for partial cohorts; they are not price-breadth counts and do not
block processing. The scope is explicitly Q2 cohort only; full-market coverage
remains `UNPROVEN`. The summary is added to the report barrier, not the Engine
market update or strategy decision.

This is an observed-fact contract, not a byte-for-byte copy of the legacy
market display rule: the old report suppresses market-level counts when
universe authority is partial. Core keeps known cohort counts visible with
their denominator and authority status; any later legacy-output adapter must
combine those facts with universe authority before filling market-level fields.

## Real replay verification

The modified runner consumed the pinned real 2026-09-29 exact-release t1-v2
Q2Frame artifact twice:

```text
input: /home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
input SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
output: /home/exedev/validation/task008-limit-state-q2frame-20261001T091203+0800/core_q2frame_opening_shadow.json
output SHA-256: 90429f5a7a69d29293fff626be0864f4a751f9ed3e96391bfe7a237db26785ee
```

```text
contract: Task008Q2FrameSessionEngineShadowV5
deterministic ordered/repeat: true
frames / updates through 09:32:10: 734 / 418,759
one Engine session: true
opening facts: READY=5,211; PARTIAL=12
observed Q2Frame cohort: 5,223; missing=0; stale=12
```

Both ordered and repeat summaries are identical. The observed cohort's
`limit_state` counts are Up=12, Normal=5,200, Down=11, all 5,223 values valid.
The 12 stale observations are retained and separately visible; all are Normal.

The reproducible pinned-artifact audit
`examples/audit_task008_real_limit_state_summary.py` compared the fresh Core
cohort to the same-run t1-v2 `opening_cutoff_v1` payload:

```text
fresh cohort / producer rows: 5,211 / 5,211
membership mismatch: 0
per-symbol limit_state mismatch: 0
fresh counts: Up=12, Normal=5,188, Down=11
present / valid / invalid: 5,211 / 5,211 / 0
audit: PASS_WITH_LIMITS
```

The producer metadata says `snapshot_integrity_status=available`, but
`universe_authority_status=partial` and has no `universe_asof_ts`. Therefore
these counts are not full-market counts. The comparison is against producer
output from the same replay, not an independent live Redis oracle.

Audit output:
`/home/exedev/validation/task008-limit-state-q2frame-20261001T091203+0800/limit_state_audit.json`.

## Verification and limits

```text
target opening/runner tests: 26 passed
full pytest: 766 passed, 3 existing protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production side effects: NONE; local frozen files only
```

This does not prove live Redis visibility, Rabbit delivery/order,
`historical_available_at`, full-market coverage, or NORMAL opening acceptance.
No live Redis/TD/Rabbit connection, producer/service change, or production
write was performed. `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` are
unaffected. No new task phase is promoted.
