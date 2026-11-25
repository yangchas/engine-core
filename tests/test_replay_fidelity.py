from __future__ import annotations

import json

from engine_core.q2 import Q2_FIELD_CONTRACT, Q2_OBSERVED_OPTIONAL_FIELDS
from engine_core.replay_fidelity import (
    TD_RETAINED_EVENT_REPLAY,
    replay_fidelity_metadata_for_input_kind,
)


def _fields_by_name(metadata: dict) -> dict[str, dict]:
    return {field["raw_name"]: field for field in metadata["fields"]}


def test_td_retained_fidelity_is_machine_readable_and_covers_q2_fields():
    metadata = replay_fidelity_metadata_for_input_kind(TD_RETAINED_EVENT_REPLAY)

    assert metadata is not None
    assert metadata["contract"] == "TDRetainedEventReplayFidelityV1"
    assert metadata["input_kind_is_caller_declared"] is True
    assert metadata["diagnostic_only"] is True
    assert metadata["blocks_downstream"] is False
    expected_fields = tuple(
        dict.fromkeys(
            [spec.raw_name for spec in Q2_FIELD_CONTRACT]
            + list(Q2_OBSERVED_OPTIONAL_FIELDS)
        )
    )
    assert tuple(field["raw_name"] for field in metadata["fields"]) == expected_fields
    assert len(_fields_by_name(metadata)) == len(expected_fields)
    assert json.loads(json.dumps(metadata)) == metadata


def test_td_retained_fidelity_preserves_observed_limits_per_field():
    metadata = replay_fidelity_metadata_for_input_kind(TD_RETAINED_EVENT_REPLAY)
    assert metadata is not None
    fields = _fields_by_name(metadata)

    assert fields["px"]["fidelity_status"] == "OBSERVED_EQUAL_AT_SAME_SOURCE_TS"
    assert fields["px"]["comparison"]["same_source_ts"] == {
        "match_symbols": 5061,
        "mismatch_symbols": 0,
    }
    assert fields["px"]["comparison"]["replay_source_newer"] == {
        "match_symbols": 87,
        "mismatch_symbols": 76,
    }
    assert fields["ia"]["comparison"]["same_source_ts"] == {
        "match_symbols": 955,
        "mismatch_symbols": 4106,
    }
    assert fields["iv"]["comparison"]["same_source_ts"] == {
        "match_symbols": 957,
        "mismatch_symbols": 4104,
    }
    assert fields["ln"]["comparison"]["same_source_ts"] == {
        "match_symbols": 4673,
        "mismatch_symbols": 388,
    }
    assert fields["mx"]["comparison"]["same_source_ts"] == {
        "match_symbols": 661,
        "mismatch_symbols": 4400,
    }
    assert fields["mn"]["comparison"]["same_source_ts"] == {
        "match_symbols": 2041,
        "mismatch_symbols": 3020,
    }
    assert fields["mk"]["comparison"]["same_source_ts"] == {
        "match_symbols": 4476,
        "mismatch_symbols": 585,
    }
    assert fields["ln"]["same_source_ts_mismatch_rate"] == round(388 / 5061, 6)
    assert fields["vol"]["unit_provenance"] == "UNKNOWN"
    assert fields["ts"]["fidelity_status"] == "CONDITIONAL"
    assert fields["ts"]["comparison"]["replay_source_newer"] == {
        "match_symbols": 0,
        "mismatch_symbols": 163,
    }
    assert fields["mk"]["fidelity_status"] == "CONDITIONAL"
    assert fields["iv"]["fidelity_status"] == "OBSERVED_DIFFERENT_AT_SAME_SOURCE_TS"
    assert fields["mx"]["fidelity_status"] == "OBSERVED_DIFFERENT_AT_SAME_SOURCE_TS"
    assert metadata["unknowns"]["rabbit_delivery_membership"] == "UNKNOWN"
    assert metadata["unknowns"]["historical_available_at"] == "UNKNOWN"
    assert metadata["density_observation"]["is_gate"] is False
    assert metadata["comparison_context"]["field_comparison_artifact"] == {
        "path": "/home/exedev/validation/task009-f-q2frame-capture-mainline-20261012/field_comparison.json",
        "sha256": "409e4153260c371904f650b7740f30e200e2ebe15f2c0efac94d13b57a660248",
    }


def test_td_fidelity_is_opt_in_and_non_td_inputs_get_no_fidelity_table():
    assert replay_fidelity_metadata_for_input_kind(None) is None
    assert replay_fidelity_metadata_for_input_kind("SEALED_REDIS_Q2_HASH") is None
    assert replay_fidelity_metadata_for_input_kind("TD_RETAINED_EVENT_REPLAY") is not None


def test_input_cohort_match_is_observational_and_never_a_gate():
    basis = replay_fidelity_metadata_for_input_kind(TD_RETAINED_EVENT_REPLAY)
    assert basis is not None
    comparison = basis["comparison_context"]

    matched = replay_fidelity_metadata_for_input_kind(
        TD_RETAINED_EVENT_REPLAY,
        run_trade_date=comparison["trade_date"],
        run_input_sha256=comparison["q2frame_sha256"],
    )
    different = replay_fidelity_metadata_for_input_kind(
        TD_RETAINED_EVENT_REPLAY,
        run_trade_date="2026-10-10",
        run_input_sha256=comparison["q2frame_sha256"],
    )

    assert matched is not None
    assert matched["evidence_cohort_matches_run_input"]["status"] == "MATCH"
    assert different is not None
    assert different["evidence_cohort_matches_run_input"]["status"] == "DOES_NOT_MATCH"
    assert different["evidence_cohort_matches_run_input"]["blocks_downstream"] is False
