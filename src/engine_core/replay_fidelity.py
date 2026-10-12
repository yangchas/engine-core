"""Non-gating fidelity notes for replaying retained TD event rows.

This module describes one observed TD→t1-v2→Q2 comparison. It does not prove
Rabbit delivery equivalence, historical availability, or universal field
behavior. The metadata is attached only when a caller explicitly declares
``TD_RETAINED_EVENT_REPLAY`` as its input kind.
"""

from __future__ import annotations

from typing import Any, Mapping

from .q2 import Q2_FIELD_CONTRACT, Q2_OBSERVED_OPTIONAL_FIELDS


TD_RETAINED_EVENT_REPLAY = "TD_RETAINED_EVENT_REPLAY"
TD_REPLAY_FIDELITY_CONTRACT = "TDRetainedEventReplayFidelityV1"

_EVIDENCE = (
    "docs/evidence/task009-t1-prefix-q2frame-vs-capture-20261011.md",
    "docs/evidence/task009-batch-semantics-and-field-differences-20261011.md",
    "/home/exedev/validation/task009-f-q2frame-capture-mainline-20261012/field_comparison.json",
    "docs/evidence/task009-j-t1-td-input-density-audit-20261012.md",
)
_FIELD_COMPARISON_ARTIFACT = {
    "path": "/home/exedev/validation/task009-f-q2frame-capture-mainline-20261012/field_comparison.json",
    "sha256": "409e4153260c371904f650b7740f30e200e2ebe15f2c0efac94d13b57a660248",
}

# (same-source-ts matches, same-source-ts mismatches,
#  replay-source-newer matches, replay-source-newer mismatches)
# Frozen from the one 2026-10-09, 5,227-symbol field comparison above.
_FIELD_COMPARISON_COUNTS: Mapping[str, tuple[int, int, int, int]] = {
    "a20": (5061, 0, 163, 0),
    "a24": (5061, 0, 163, 0),
    "a25": (5061, 0, 163, 0),
    "am": (5061, 0, 163, 0),
    "amt": (5061, 0, 29, 134),
    "amt2m": (5061, 0, 29, 134),
    "amt5m": (5061, 0, 29, 134),
    "ar": (5061, 0, 163, 0),
    "br": (5061, 0, 163, 0),
    "ia": (955, 4106, 29, 134),
    "iv": (957, 4104, 29, 134),
    "ln": (4673, 388, 152, 11),
    "ls": (5061, 0, 163, 0),
    "mk": (4476, 585, 130, 33),
    "mn": (2041, 3020, 57, 106),
    "mx": (661, 4400, 25, 138),
    "pc": (5061, 0, 163, 0),
    "ph": (5061, 0, 163, 0),
    "px": (5061, 0, 87, 76),
    "spd1m": (5061, 0, 87, 76),
    "ts": (5061, 0, 0, 163),
    "vec3m": (5061, 0, 87, 76),
    "vec5m": (5061, 0, 87, 76),
    "vol": (5061, 0, 29, 134),
}

_FIELD_STATUS: Mapping[str, tuple[str, str]] = {
    **{
        name: (
            "OBSERVED_EQUAL_AT_SAME_SOURCE_TS",
            "The frozen comparison matched for this field in the same-source-timestamp subset; this is one date/capture, not stream-equivalence proof.",
        )
        for name in (
            "px", "pc", "amt", "vol", "ph", "ls", "a20", "a24", "a25",
            "am", "br", "ar", "amt2m", "amt5m", "spd1m", "vec3m", "vec5m",
        )
    },
    "ts": (
        "CONDITIONAL",
        "5,061 symbols had equal source time; 163 replay source timestamps were newer than the captured state; 3 symbols had no replay state.",
    ),
    "mk": (
        "CONDITIONAL",
        "Raw market labels differed for 585 same-source-time symbols; observed examples include replay kc versus captured sh. Equity-classifier acceptance does not make raw labels equivalent.",
    ),
    **{
        name: (
            "OBSERVED_DIFFERENT_AT_SAME_SOURCE_TS",
            "This single captured comparison shows same-source-time differences; it does not prove permanent non-reconstructibility or establish one cause.",
        )
        for name in ("iv", "ia", "ln", "mx", "mn")
    },
}


def _ordered_raw_fields() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            [spec.raw_name for spec in Q2_FIELD_CONTRACT]
            + list(Q2_OBSERVED_OPTIONAL_FIELDS)
        )
    )


def build_td_retained_event_replay_fidelity() -> dict[str, Any]:
    """Return JSON-serializable, explicitly non-gating field-fidelity evidence."""

    specs_by_name = {spec.raw_name: spec for spec in Q2_FIELD_CONTRACT}
    fields: list[dict[str, Any]] = []
    for raw_name in _ordered_raw_fields():
        spec = specs_by_name.get(raw_name)
        status, note = _FIELD_STATUS.get(
            raw_name,
            (
                "UNASSESSED",
                "No field-specific comparison is recorded in this evidence set.",
            ),
        )
        counts = _FIELD_COMPARISON_COUNTS.get(raw_name)
        field: dict[str, Any] = {
            "raw_name": raw_name,
            "fidelity_status": status,
            "note": note,
            "evidence": list(_EVIDENCE[:3]),
        }
        if spec is not None:
            field.update(
                {
                    "canonical_name": spec.canonical_name,
                    "unit": spec.unit,
                    "semantic": spec.semantic,
                }
            )
        if raw_name == "vol":
            field["unit_provenance"] = "UNKNOWN"
            field["unit_note"] = (
                "Observed values matched in the same-source-time subset, but the upstream unit remains unresolved."
            )
        if counts is not None:
            same_match, same_mismatch, newer_match, newer_mismatch = counts
            same_source_ts_total = same_match + same_mismatch
            field["comparison"] = {
                "cohort": "2026-10-09 captured Q2 symbol cohort",
                "same_source_ts": {
                    "match_symbols": same_match,
                    "mismatch_symbols": same_mismatch,
                },
                "replay_source_newer": {
                    "match_symbols": newer_match,
                    "mismatch_symbols": newer_mismatch,
                },
                "no_replay_state_symbols": 3,
            }
            field["same_source_ts_mismatch_rate"] = (
                round(same_mismatch / same_source_ts_total, 6)
                if same_source_ts_total
                else None
            )
        fields.append(field)

    return {
        "contract": TD_REPLAY_FIDELITY_CONTRACT,
        "input_kind": TD_RETAINED_EVENT_REPLAY,
        "input_kind_is_caller_declared": True,
        "diagnostic_only": True,
        "blocks_downstream": False,
        "comparison_context": {
            "trade_date": "2026-10-09",
            "q2frame_sha256": "7474de28bfc60e8777cef0f359b4f6d2ab1b4f1ab6bcdf4fee5211a721483467",
            "capture_symbols": 5227,
            "same_source_ts_symbols": 5061,
            "replay_source_newer_symbols": 163,
            "no_replay_state_symbols": 3,
            "scope": "one final-state comparison; no intermediate-event comparison",
            "field_comparison_artifact": dict(_FIELD_COMPARISON_ARTIFACT),
        },
        "density_observation": {
            "window_start_local": "09:15:00",
            "window_end_local_approx": "09:39:31",
            "live_source_in": 2478545,
            "live_conversion_rejects": 76295,
            "live_accepted_engine_ticks": 2402250,
            "td_retained_rows": 1147924,
            "source_in_to_td_rows_approx": 2.16,
            "accepted_ticks_to_td_rows_approx": 2.09,
            "cause_attribution": "PARTIAL",
            "is_gate": False,
            "evidence": [_EVIDENCE[3]],
        },
        "fields": fields,
        "unknowns": {
            "rabbit_delivery_membership": "UNKNOWN",
            "rabbit_arrival_order": "UNKNOWN",
            "historical_available_at": "UNKNOWN",
            "same_symbol_same_timestamp_collision_count": "UNKNOWN",
            "full_market_completeness": "UNKNOWN",
            "exact_per_event_replay": "UNPROVEN",
        },
        "non_claims": [
            "not exact Rabbit stream or batch reproduction",
            "the runner input-kind label is caller-declared and does not validate artifact provenance",
            "not historical available_at or arrival-order evidence",
            "not full-market or universal field-parity proof",
            "not a production or NORMAL acceptance gate",
            "not a claim that every accepted live tick should become one retained TD row",
        ],
        "evidence": list(_EVIDENCE),
    }


def replay_fidelity_metadata_for_input_kind(
    input_kind: str | None,
    *,
    run_trade_date: str | None = None,
    run_input_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Return the TD fidelity table only for the explicit TD replay input kind."""

    if input_kind != TD_RETAINED_EVENT_REPLAY:
        return None
    metadata = build_td_retained_event_replay_fidelity()
    expected = metadata["comparison_context"]
    date_match = (
        None
        if run_trade_date is None
        else run_trade_date == expected["trade_date"]
    )
    sha_match = (
        None
        if run_input_sha256 is None
        else run_input_sha256 == expected["q2frame_sha256"]
    )
    if date_match is False or sha_match is False:
        cohort_status = "DOES_NOT_MATCH"
    elif date_match is True and sha_match is True:
        cohort_status = "MATCH"
    else:
        cohort_status = "UNVERIFIED"
    metadata["evidence_cohort_matches_run_input"] = {
        "status": cohort_status,
        "trade_date_match": date_match,
        "input_sha256_match": sha_match,
        "run_trade_date": run_trade_date,
        "run_input_sha256": run_input_sha256,
        "diagnostic_only": True,
        "blocks_downstream": False,
    }
    return metadata
