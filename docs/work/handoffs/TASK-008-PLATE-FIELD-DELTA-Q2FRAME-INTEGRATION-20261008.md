# TASK-008 plate field-delta / Q2Frame integration audit

Date: 2026-10-08 (Asia/Shanghai)

## Result

`FEATURE_INTEGRATION=PASS_WITH_LIMITS`
`TASK_008=PARTIAL_EVIDENCE`
`NORMAL_OPENING_ACCEPTANCE=UNPROVEN`

This change adds a separate, fact-only plate field-delta sidecar to the
existing Task-008 Q2Frame shadow report. It does not feed the Engine,
reducer, strategy, or existing directional-pressure path.

## Real-data evidence

Validation directory:

`/home/exedev/validation/task008-plate-field-delta-integration-20261008T035838+0800/`

- `plate_field_delta_context.json` SHA-256:
  `4f8816e6b7165bf72cc3781cd84f471aefbcde07a84e5de5cac02ff5338bfd12`
- Sidecar content hash:
  `092e398fe6d649b849c952bcff6ba7ded4eb477d9430d4c43e90c2635d497224`
- Field summary content hash (ordered and repeat run):
  `8011496b2848d4404daa52cbc22c4453471d38cdeb4735e3ef542de5e302ff2f`
- `core_q2frame_report.json` SHA-256:
  `c1ddf44ac5534043f6cdb745e54f8f18e32463e15439ae2d0fc548c8d0371faa`
- Q2Frame input SHA-256:
  `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`
- TD source SHA-256:
  `b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b`
- Frozen mapping SHA-256:
  `c88eb9339fb1a30dbf6c82da41eebca12f8128a399bd045eaa3da4fbf1a4553b`

The sealed 2026-09-29 source cohort contained 5,223 symbols. The frozen
450-plate map contained 5,963 mapped symbols; overlap was 5,155, mapping-only
was 808, and capture-only was 68. For the selected 10-plate context, the
upstream context carried 1,321 members while the frozen mapping had 1,412.
The 91 map-only members are all absent from the captured TD cohort. The
sidecar preserves this hash-pinned observed-cohort scope; it must not be
described as complete coverage of those 10 plates. Field-specific zero values
remain valid observations.

An independent raw recomputation matched all per-plate numeric aggregates,
counts, coverage, and statuses (zero mismatches). Reversing fact and mapping
input order preserved the deterministic content hash. The full same-date
Q2Frame replay consumed 734 frames / 418,759 updates through 09:32:10;
5,211 opening facts were READY and 12 PARTIAL. Per-symbol anchor evidence in
this report was 1,064 AVAILABLE / 4,159 MISSING at 09:20, 3,137 AVAILABLE /
2,086 MISSING at 09:24, and 5,070 AVAILABLE / 153 MISSING at 09:25. These
counts describe captured Q2Frame-derived anchor facts, not live freeze
visibility. The replay was deterministic.

Compared with the prior same-date baseline, Q2Frame input hash, frame/update
counts, Engine final-state hash (`59676df3623ed2248d67980adf28dbea325fe290350fb38e27dc18087c0e6e27`),
per-symbol opening fact hash, transition summary, price summary, and legacy
directional-pressure summary were unchanged. Thus the sidecar did not alter
existing Engine or fact paths in this run.

## Same-date 2026-09-30 confirmation

A second audit used sealed 2026-09-30 TD rows and the complete frozen mapping,
then ran the Core Q2Frame report with both the existing pressure sidecar and
the new field-delta sidecar.

- TD auction rows: 5,215 at 09:24 and 5,220 at 09:25; source symbol count
  5,220. Frozen mapping: 5,964 members across 450 plates; 5,151 overlap the
  TD cohort, 813 are mapping-only, and 69 captured symbols are not in the map.
  There are 5,146 symbols with both anchor rows; five observed mapped symbols
  lack one or more required anchor values.
- For each of amount, resting bid, resting ask, and book pressure, independent
  raw-row recomputation found 5,146 available mapped-symbol facts, five
  missing anchor values, and 813 symbols absent from the TD cohort; zero
  invalid values and zero raw parity mismatches. Plate statuses were 373
  available, 67 partial, and 10 unavailable. `full_market_coverage` remains
  `UNPROVEN`.
- Combined Core report:
  `/home/exedev/validation/task008-field-delta-same-date-audit-20261008T044210+0800/core_q2frame_with_pressure_and_field_delta_20260930.json`
  (SHA-256 `3d1000b522d960bd0081fbd657d907ffbda43dbb5db7ac6b3ae84e366359daa2`).
  Field-delta summary hash:
  `eaa28325e3e4523eb33aa9599a98da9d05db058498a03364fd6377762d88c76c`.
  Ordered and repeat runs agree. Against the prior same-date baseline, the
  Q2Frame hash, 754 frames, 428,586 updates, one Engine, 758 signals, revision
  754, virtual clock, Engine final hash, opening-facts hash/status, and legacy
  pressure summary hash are identical.
- The same-date t1-v2 auction-command capture records an
  `atomic_cutoff_publish` at 09:32:10 with 5,213 rows accepted, zero missing
  and zero invalid rows, and payload SHA-256
  `8282bf5ce1f7b919f6705cd6ad05324b846ae47f96c98878f3b17df2405fade7`.
  Core reports 5,213 READY and 7 PARTIAL opening facts over the 5,220-symbol
  Q2Frame cohort. This is aggregate count agreement only: the captured command
  contains metadata, not payload rows, so it cannot establish per-symbol or
  value parity. The producer marks universe authority `partial` and leaves
  `universe_asof_ts` empty. Core's seven PARTIAL facts are exactly the seven
  symbols whose source age exceeds its 60-second freshness policy at the
  09:32:10 evaluation; therefore the 5,213/5,213 count agreement is consistent
  with excluding that stale tail. Without the producer payload rows, this
  cannot prove the same seven symbols were excluded by t1-v2.
- No same-date 09:40 producer snapshot was found among the retained
  2026-09-30 validation artifacts. The 09:32 record is metadata, not a 09:40
  snapshot. No same-date 09:26 producer barrier sidecar was supplied to this
  09:32 Core report. Do not synthesize either cutoff from a later `latest`
  state.
- The full 2026-09-30 Core run took about 35 minutes. This is a runtime
  observation, not a correctness failure or production-acceptance gate.

## Verification

- Full test suite: 854 passed; 3 existing protobuf/upb deprecation warnings.
- `compileall`: PASS.
- `git diff --check`: PASS.
- Side effects: `NONE_OBSERVED`; validation used frozen local artifacts and
  did not access live Redis, TDengine, RabbitMQ, Wencai, or production services.

## Limits and follow-up

- `full_market_coverage=UNPROVEN`; the sidecar only describes its declared
  cohort. On 2026-09-29 the selected context is narrower than the frozen map
  because 91 selected-plate members are absent from the TD capture. On
  2026-09-30 the full frozen map is used, but 813 mapping members have no TD
  cohort row.
- Historical availability, producer attestation, Rabbit arrival ordering,
  and NORMAL opening equivalence remain unproven.
- This is not live-data acceptance and does not change M3-1:
  `M3_1_NORMAL=BLOCKED`, `TD_WRITE_HEALTH=UNPROVEN`.
- TASK-008 remains `PARTIAL_EVIDENCE`; this feature-scoped audit does not
  close the task or authorize moving to the next production phase.
