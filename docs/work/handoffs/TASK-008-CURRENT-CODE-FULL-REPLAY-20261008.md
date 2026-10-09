# TASK-008 — current Core code on the full 2026-09-29 opening replay

Audit date: 2026-10-08 (Asia/Shanghai)\
Result: `DETERMINISTIC_PARTIAL_REAL_REPLAY`

## Scope and result

The current Core working tree replayed the pinned real t1-v2 Q2Frame stream
through the 09:32:10 opening evaluation, with the same-date captured amount,
price-reference, and auction-pressure contexts. The runner performed an
ordered pass and a repeat pass, each with one Engine instance.

```text
Q2FRAME_INPUT_HASH=PASS
ORDERED_REPEAT_DETERMINISM=PASS
OPENING_FACTS=5211_READY_12_PARTIAL
AUCTION_PRESSURE=10_PARTIAL_PLATES
FULL_MARKET_COVERAGE=UNPROVEN
RABBIT_ARRIVAL_AND_HISTORICAL_AVAILABLE_AT=UNKNOWN
NORMAL_OPENING_ACCEPTANCE=NOT_EVALUATED
TASK-008=PARTIAL_EVIDENCE
PRODUCTION_SIDE_EFFECTS=NONE_OBSERVED
```

## Frozen real inputs

All source files below are historical validation artifacts; this run did not
connect to TDengine, Redis, RabbitMQ, or a production service.

| Input | SHA-256 | Contract/content hash or scope |
|---|---|---|
| `deployed_release_q2frame_to_0932.jsonl` | `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9` | 2026-09-29 t1-v2 Q2Frame |
| `td_auction_snapshot_rows.jsonl` | `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b` | Captured TD 0920/0924/0925 rows |
| `stock_plate_snapshot.json` | `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b` | Frozen same-date plate mapping |
| `opening_plate_amount_context.json` | `7c5f01017174f96689ab3d0f3e8cf3cdfbd8da57eb4546eec17fb1b256e3f1c6` | `OpeningPlateAmountContextV1`, content hash `aefeada38756163cfd01d57bcb125c27f4e55d2387fce0aa7816fe0b092cd2c0` |
| `opening_plate_price_reference_context.json` | `ec7f199b184924b676e7f9b04b5990484ff3a8b1d0519895b3d656ee114c9981` | `OpeningPlatePriceReferenceV1`, content hash `d2d5b18e363a93752dbe4f2dc994876d49db46a3c1584b57468463905072c11b` |
| `opening_plate_auction_pressure_context.json` | `25b79753fc3f00c2cf40c15f84c86bb6f4653355c7fef429f133f6e0a27f3027` | `OpeningPlateAuctionPressureContextV1`, content hash `2471a2dc854e6177fcd3e73e1aa6fc351a9952fdc5b0bc4e237729728727b0d0` |

## Replay evidence

Output:
`/home/exedev/validation/task008-0929-current-core-replay-20261008T005739+0800/core_q2frame_report.json`\
Output SHA-256: `4474dc28d8a3ff24890963a0cd4481970d65cfe4431be4237d72885624f133de`

```text
input artifact: 735 non-empty frames, 419,533 updates, 5,223 symbols
processed through 09:32:10: 734 frames, 418,759 updates
excluded: one input frame after the configured opening evaluation time
Engine instances: 1 per pass
signals / reducer revision: 738 / 734 per pass
ordered and repeat final state hash:
  59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27
ordered and repeat strategy result hash:
  a3e14b9b926d738d360787dd379d153bbf85b767f1955d8b5ba875ea0e8d13f4
opening fact statuses: READY=5,211; PARTIAL=12
opening observed-cohort coverage: 1.0; full-market coverage: UNPROVEN
```

Standalone auction-anchor facts retained their actual availability:

| Anchor | Available | Missing |
|---|---:|---:|
| 09:20 | 1,064 | 4,159 |
| 09:24 | 3,137 | 2,086 |
| 09:25 | 5,070 | 153 |

All ten selected plate pressure facts are emitted as `PARTIAL`; observed
coverage ranges from about 0.385 to 0.810. This is partial fact output, not a
full-market or production report acceptance claim. The separate 2026-10-08
pure-helper audit found pressure values/statuses matching the pinned
engine-next helper on all ten plates, but denominator parity only on two; see
`TASK-008-OPENING-PRICE-PRESSURE-CURRENT-CODE-20261008.md`.

The previous 2026-10-01 full replay had the same final state hash and opening
price values. Its price-summary hash (`9bc2d45b…`) differs from the current
hash (`b2878ba0…`) because the current contract includes limit-state status
and count fields; a field-level comparison found the existing price values
unchanged. The current run additionally includes the captured pressure
context.

## Runtime and verification

The runner took approximately 33 minutes wall time in this environment and
used about one CPU core; observed RSS stayed near 175 MB and `/home/exedev`
had about 14 GB free. Runtime is recorded as a performance observation only,
not a replay failure or acceptance gate. The runner does not include an
elapsed-time field in its JSON report.

On the same unchanged working tree, verification was:

```text
pytest: 834 passed, 3 protobuf/upb deprecation warnings
compileall: PASS
git diff --check: PASS
```

Core source provenance at execution:

```text
branch: codex/feature-session-engine-integration
HEAD: a18654e10af663c48dc0571c5baa6c457baebaf0
worktree: dirty; this replay made no code changes and no commit was created
runner SHA-256: 55c025c43292d7c4d2d2b0670345dedd9b197d727129eb61224776ab27f69312
opening.py SHA-256: 8f80976f8641849c6daa81e9078db5ef28dc27210b658151773e46e9b94c3c4c
auction_timeline.py SHA-256: 26445d570cb7c1c2c4d22f0dca5d1a2e0d1ae046236fd6035095ec4bac260573
anchor_delta.py SHA-256: 5c344d43fe7d961440b0fe36935508975374fcd7e1faa9bedf3bd31d0dba4295
```

## Limits and next step

- This validates the current Core against pinned historical TD/t1-v2-derived
  artifacts. It does not re-run t1-v2 from TD in this invocation, prove the
  producer binary that emitted the retained Q2Frame, or compare a producer
  09:32:10 barrier snapshot; it is not a live Rabbit replay.
- Rabbit delivery/member/arrival order and historical Redis `available_at`
  remain `UNKNOWN`. The 09:25 recovery plan is not evidence of provider
  execution.
- Partial 09:20/09:24/09:25 anchors remain visible and do not suppress
  independent opening facts. Do not upgrade `PARTIAL` to `READY` or infer
  missing values as zero.
- Use the existing 2026-09-30 sealed capture to review whether the
  engine-next all-or-nothing plate-universe gate should have a bounded partial
  output behavior. Keep that as a feature-scoped policy review; do not change
  producer/service behavior or make it a global Core gate.
- `TASK-008` remains `PARTIAL_EVIDENCE`; `M3_1_NORMAL=BLOCKED` and
  `TD_WRITE_HEALTH=UNPROVEN` are unchanged.
