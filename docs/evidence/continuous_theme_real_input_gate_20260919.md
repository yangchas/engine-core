# Continuous theme real-input gate — 2026-09-19

## Purpose

Close the boundary between a real postmarket theme observation and the
continuous Core Engine evaluation. This check must prove that non-empty real
theme facts do not become a historical runtime fact when `available_at` is
unknown.

## Real source evidence

The existing read-only Redis theme artifact was used without modification:

```text
source: /home/exedev/validation/engine-core-47d4392/theme-strategy-shadow-20260918.json
sha256: ae8857c4b730bde06b099827dbc9ffc5d43be1b45a991251dac001b39714d8a6
trade_date: 2026-09-18
0924 projection: READY
0925 projection: READY
facts: 119
mapping_count: 170
observed_at_ms: 1789737743211
```

The artifact is a postmarket observation. It does not contain historical
publication/availability proof for the 09:25:06 cutoff.

## Core boundary execution

The 119 real facts were placed in one existing `DataResult` with:

```text
function_id = theme_auction_delta_compat
status = UNAVAILABLE
available_at_ms = None
missing_fields = (available_at_unknown,)
temporal_mode = HISTORICAL
```

That result was submitted through the current continuous session entry point.
No aggregation, mapping lookup, Redis read, or fallback was added to Core.

Remote command used in the isolated exact-commit directory:

```text
PYTHONPATH=src:examples \
  /home/exedev/services/engine-next/shared/venv/bin/python \
  run_real_theme_rejection_boundary.py
```

Artifact:

```text
/home/exedev/validation/engine_core-ecc-9205894/real_theme_rejection_boundary_20260919.json
sha256: 47b2c292f05f56dcb7609fcf31387d1e6e9c03afb3655601ea37579f636104c6
```

Observed result:

```text
theme_data_result_status = UNAVAILABLE
theme_available_at_ms = None
theme_shadow_promoted = false
theme_shadow.reason_codes = [THEME_DATA_NOT_READY]
theme_data_ready_count = 1
auction_fact_status = PARTIAL
opening_status = READY
processed_signals = 9
read_only = true
```

The same existing auction/opening harness used frozen Core fixture inputs
because no same-date (2026-09-18) Q2 raw capture was available in the
validation archive. This is explicitly a boundary harness, not a claim of a
full same-day real-input composition.

## Acceptance

```text
real_input_identity_closed                    PASS
real_theme_unknown_availability_not_promoted  PASS
current_evaluation_binding                    PASS
unavailable_theme_does_not_change_other_facts PASS
runtime_rule_not_called_for_unavailable       PASS (shadow is None)
production_side_effects_zero                  PASS
real_theme_runtime_semantics                  NOT_VERIFIED
same-day full auction/Q2 composition          NOT_VERIFIED (missing Q2 artifact)
```

The source `observed_at_ms` was retained as evidence only. It was not used to
invent `available_at_ms`, and no historical 09:25:06 runtime claim was made.
Production services remained active; no Rabbit consumer/ACK, Redis/TD write,
notification, or effect was introduced.
