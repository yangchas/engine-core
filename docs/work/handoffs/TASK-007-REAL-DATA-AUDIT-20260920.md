# TASK-007 real-data read-only audit

审计范围：ordered/shuffled 500-frame validation evidence、runner 边界和 Core
import boundary。此报告是主 Codex 的只读审计，不替代独立 auditor。

## Evidence checks

```text
ordered validation      /home/exedev/validation/task007-real-ordered-20260920T154654+0800/
shuffled validation     /home/exedev/validation/task007-real-shuffled-20260920T161558+0800/
ordered sha256sums      PASS
shuffled sha256sums     PASS
determinism             PASS (500 frames)
side effects            NONE_OBSERVED
```

Both runs independently read TD frames using one `SELECT` per 3-second
half-open interval. Each completed with 500 frames, 1,224,811 rows, 5,221
symbols, 98 empty frames, 500 signals, reducer revision 500, and VirtualClock
at 09:40 Asia/Shanghai. The comparison matched frame count, signal hash,
session content hash, reducer revision, final clock, and 0920/0924/0925 auction
revisions.

## Boundary checks

- Validation artifacts contain no credentials or raw authentication content.
- `side_effect_audit.json` records TD `DESCRIBE`/`SELECT` only; Redis, Rabbit
  consume/ACK, service restart and effects are false.
- `src/engine_core` has no `taos`, Rabbit client, Redis client or writer import.
- The TD client import exists only in the explicit validation example.
- `source_sequence`, Rabbit arrival order and historical `available_at` remain
  `UNKNOWN`; shuffled order is not historical Rabbit arrival evidence.
- `production_equivalence` remains `UNPROVEN`.

## Result

```text
TASK_007_REAL_DATA_FUNCTIONAL=PASS
TASK_007_REAL_DETERMINISM=PASS
TASK_007_SIDE_EFFECT_AUDIT=PASS
TASK_007_PERFORMANCE=OPTIMIZATION_REQUIRED (not a functional failure)
INDEPENDENT_AUDITOR=REQUIRED
```

