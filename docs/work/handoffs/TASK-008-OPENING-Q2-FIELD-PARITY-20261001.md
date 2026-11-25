# TASK-008 opening Q2 field parity — 2026-10-01

## Result

```text
CORE_PRICE_AND_CHANGE_PARITY=PASS_WITH_LIMITS
CORE_AMOUNT_2M_PARITY=PASS_WITH_LIMITS
CORE_LIMIT_STATE_PARITY=PASS_WITH_LIMITS
CORE_SPEED_1M_UNIT_PARITY=PASS_WITH_LIMITS
LEGACY_Q2FRAME_HELPER_SPEED_FIELD=UNIT_MISMATCH
SOURCE_VS_FRAME_TIME=PASS_WITH_LIMITS
TASK-008=PARTIAL_EVIDENCE
```

This is a local comparison of pinned real historical artifacts. It did not
connect to Redis, TDengine, or RabbitMQ and did not modify engine-next or
production behavior.

## Pinned inputs

Real t1-v2 Q2Frame:

```text
/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl
SHA-256: 5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9
producer binary SHA-256: 363685f830c62aa3a2a8321eb93f91c5c7babccbd74c5e67b1e5b4c2dad1ab56
```

Core Q2Frame opening report, evaluated through the whole-second cutoff
09:32:10:

```text
/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/integrated_core_q2frame_report.json
SHA-256: fe1b04b0956dff3593ec6b157c60a68bd23004c1bd9a14e853940f8432bb921b
```

Pinned engine-next release files inspected:

```text
open_confirmation.py SHA-256: 73453f5caaf27636c9ad6aa57b2e609da0ad1867ebc1ff06906eca46921405b6
intraday_data_hub.py SHA-256: 645ab676d921c6437b28efce3535b4ee32cac8483334610c5dc30e532f634c5b
```

## 09:32 observed-cohort comparison

Across 5,223 symbols from the same frozen source and cutoff:

- Legacy helper rows and Core facts both contained 5,223 symbols.
- Price/pre-close inputs matched 5,223/5,223; derived `change_pct` matched
  5,223/5,223 under the same formula.
- Q2 `amt2m` / Core `amount_2m_yuan` matched 5,223/5,223.
- Q2 `ls` / Core `limit_state` matched 5,223/5,223.
- Core opening `speed_1m` matched `Q2.spd1m / 10000` for 5,223/5,223.
- All 5,223 source `spd1m` fields were present; 1,133 were zero and 4,090
  nonzero.

The unit distinction is confirmed by source contracts: t1-v2 calculates and
serializes `spd1m` as integer basis points. The pinned
`IntradayDataHub._standardize_q2_quote` converts it to a decimal ratio and also
exposes a separate `speed_1m_bp`; Core's `OpeningShadowStrategy` likewise
converts basis points to a ratio. In contrast, the pinned
`open_confirmation._open_rows_from_frames` helper copies raw `spd1m` directly
into a field named `speed_1m`. It does this for 5,223/5,223 rows. A naive
comparison therefore reports 4,090 apparent nonzero mismatches (e.g. `8` vs
`0.0008`); this is a helper-unit mismatch, not evidence that Core's conversion
is wrong. The helper's returned `fields` list also omits `speed_1m`, so that
metadata must not be read as a complete inventory of values in each row.

This does **not** establish that the production opening strategy consumes that
helper's raw value. It establishes only that this helper is not a valid
decimal-ratio oracle for `speed_1m` without normalization. No production
engine-next file was changed.

## Time lineage at the cutoff

For the same 5,223 symbols, Core per-symbol source `timestamp_ms` equaled the
legacy helper's frame `timestamp_ms` for 5,211. For 12 symbols the source time
was earlier than the frame time; all 12 are in the stale cohort. For example,
`000016` was emitted in the 09:30 frame with a 09:15 source time. These are
source-event-time versus frame-logical-time differences, not Rabbit arrival
latency. Core retains the source time; frame/evaluation time remains available
at the opening snapshot level.

## Regression and verification

A regression now drives the pinned real 2026-09-29 Q2 update for `000002`
through `normalize_q2` and `OpeningShadowStrategy`; its raw `spd1m=26` remains
basis points internally and the emitted opening fact is `speed_1m=0.0026`,
matching the frozen engine-next standardized Q2 view. No calculation code
changed.

```text
focused replay/Q2/opening tests: 51 passed
full Core suite: 786 passed, 3 upstream protobuf deprecation warnings
compileall: PASS
git diff --check: PASS
production I/O and side effects: NONE
```

## Alignment

The next consumer comparisons must normalize fields by their actual contracts
before computing parity. Do not rename basis points as a ratio, and do not
change Core's correct conversion to reproduce the legacy helper's raw pass-
through. Keep `timestamp_ms` comparisons separated by source-time versus
frame-time meaning. These results remain bounded to this pinned date, release,
cutoff, and observed cohort; Rabbit arrival, historical availability, and
full-market authority remain unknown.
