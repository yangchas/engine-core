# Real Redis/TD Core shadow — 2026-09-16 11:54 CST

## Scope

This was a bounded, read-only run from the exact Core code archive used for
the current validation. It used the existing Redis Q2 read path and existing
TD auction projection comparison path. No Rabbit consumer was added, no ACK
was changed, and no Redis/TD writer or notification path was assembled.

## Redis Q2

```text
requested symbols: 5221
returned symbols: 5221
coverage: 1.0
status: PARTIAL
consistency: BEST_EFFORT_PARTIAL
stale symbols: 5221
newest source lag: about 2104 seconds
same-observation Engine result: deterministic
projection hash: 1058163e464eaef16c7d75c4251acb1e09cbf2879e6a9e072c6febf90dcbc82f
```

The full active cohort was therefore present but not fresh under the explicit
60-second validation budget. Coverage is not promoted to READY.

## Opening facts

The same four bounded symbols were parsed from the real Q2 projection. The
batch result remained `PARTIAL` with `STALE_OR_MIXED` freshness and coverage
`1.0`. Field-level opening facts were available for the sample, but that does
not override the stale batch quality. The Core output remained fact-only and
did not infer a strategy decision.

## Redis/TD auction comparison

```text
symbols: 000001, 300207, 600330, 600519
tags: 0920, 0924, 0925
MATCH: 0
MISMATCH: 0
PARTIAL_COMPARABLE: 6
NOT_COMPARABLE: 6
timestamp mismatches: 0
```

The non-comparable rows are retained as such because Redis is a Top-N or
partial projection for the selected symbol/tag, and some anchor fields are
absent. Matching values are only claimed for the authority-matrix fields; no
value was filled from another semantic field.

## Runtime and safety

```text
code probe archive: engine-core-806659fd77cc5115a519fe375fea1099f90c9de6
remote output dir: /home/exedev/validation/core-shadow-20260916-1200
local copied evidence: tmp/real-live-20260916-1200/
observed wall time: 2026-09-16T11:54 CST
engine-next: active, NRestarts=0
t1-v2-live: active, NRestarts=0
root disk: 19G total, 2.2G available (88% used)
Redis/TD/Rabbit writes: 0
```

This is real-source shadow evidence, not production replacement acceptance.
The stale Q2 source and partial cross-source comparability remain explicit
warnings for the replacement gate.

