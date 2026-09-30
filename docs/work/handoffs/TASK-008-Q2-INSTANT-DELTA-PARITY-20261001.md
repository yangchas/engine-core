# TASK-008 Q2 instant-delta contract repair — 2026-10-01

## Outcome

Core now exposes t1-v2 Q2 fields `iv`, `ia`, and `ln` as typed facts:

- `instant_volume_lots`: event-level volume delta, same unit as Q2 `vol` (lots);
- `instant_amount_yuan`: event-level amount delta (yuan);
- `large_net_yuan`: signed event-level large-net amount (yuan).

Missing values remain `None`, explicit zero remains `0`, and malformed values
remain `None` with the raw field listed in `field_errors`. The projection
contract identifier was bumped to `Q2CanonicalProjectionV3`. No strategy,
producer, source-selection, or production behavior was changed.

## Real-data check

The adapter was compared row-by-row with the current development
`IntradayDataHub._standardize_q2_quote` Q2 fallback mapping using the frozen
2026-09-29 Q2Frame artifact:

```text
artifact: /home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
artifact SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
engine-next source commit: b2aa169c3dddf5d5c7c452f4893dcc01848e358f
engine-next consumer file SHA-256: 7ba1ff82324314ccc400e52c96bb34c423002672a0d6eeb1fd892f36fdb43936
t1-v2 source files at that commit:
  C/t1_v2/tick_delta_calculator.cpp SHA-256 b882a53f1cabc54efe1d607a9e6a64ae731bf97e504562794afcbb9e857e23ad
  C/t1_v2/q2_projection.cpp SHA-256 4af79a114bb32aa0d1bf10bb8dfe41a774dddead8bea19d15e2d073dedcececa
  C/t1_v2/redis_v2_writer.cpp SHA-256 b3d1c744db4a2d1d43bfff120511a9d8c21221b70f40a61c9f2f3a63d8a5f63b
frames: 735
Q2 updates: 419,533
normalized field comparisons: 7,551,594
```

No value differences were found for the 18 compared normalized fields except
the intentional missing/default distinction below. The new delta fields were
present on every update; nonzero rows were `iv=202,566`, `ia=202,610`, and
`ln=33,843`.

In 242 updates, all three source fields `am`, `br`, and `ar` were absent. The
legacy consumer standardizer turns those absent values into `0`; Core keeps
them `None`. This is not reported as exact parity: it is an explicit
`MISSING != ZERO` difference retained by the Core contract. On present Q2
auction fields, Core matches the consumer's `q2_*` values. The separate
phase-gated generic auction values remain distinct by design.

## Limits

- This used a frozen historical artifact, not a fresh Redis read.
- Comparison covers the Q2 fallback standardizer, not legacy
  `stock:quote:*` precedence or the entire live engine-next decision path.
- This mapping comparison does not assert that a row's source `ts` equals its
  containing Q2Frame logical time or establish a freshness threshold; those
  remain separate time facts.
- No Rabbit/TD access, Redis/TD writes, service changes, or strategy evaluation
  occurred.
- This proves mapping compatibility for this recorded Q2Frame corpus; it does
  not prove live/replay equivalence or production behavior.
- The pinned positive-delta row also passes through
  `build_q2_projection`; changing only its `ln` value changes the projection
  content hash, confirming the newly typed delta facts participate in replay
  projection identity.

## Verification

```text
targeted Q2 tests: 4 passed
full pytest: 758 passed
compileall: PASS
git diff --check: PASS
```

## Handoff

This closes the typed-field preservation gap only. The fields are available to
future Core consumers, but no strategy is authorized by this change to treat
them as decision inputs. Preserve the missing/default distinction and keep
live-consumer parity bounded until legacy-source selection and a same-run live
comparison are evidenced.
