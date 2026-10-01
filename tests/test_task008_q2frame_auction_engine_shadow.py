from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine_core import build_opening_plate_amount_context
from examples.run_task008_q2frame_auction_engine_shadow import (
    _inventory,
    run_q2frame_auction_engine_shadow,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _epoch_ms(clock: str) -> int:
    value = datetime.fromisoformat("2026-09-18T" + clock).replace(tzinfo=SHANGHAI)
    return int(value.timestamp() * 1000)


def _frame(seq_no: int, clock: str) -> dict:
    timestamp_ms = _epoch_ms(clock)
    updates = []
    for index, (symbol, market) in enumerate((("000001", "sz"), ("600000", "sh"))):
        updates.append(
            {
                "symbol": symbol,
                "mk": market,
                "name": "fixture",
                "px": 10_000 + index * 100,
                "pc": 10_000,
                "amt": 1_000_000 + seq_no * 100 + index,
                "vol": 100 + seq_no,
                "ts": timestamp_ms,
                "ph": 1,
                "ls": 0,
                "am": 500_000 + seq_no * 10 + index,
                "br": 300_000 + seq_no * 5 + index,
                "ar": 200_000 + seq_no * 3 + index,
                "amt2m": 10_000 + seq_no * 100 + index,
                # Q2's live px continues changing; the captured t1-v2 auction
                # anchors are separate fields and must drive auction prices.
                "a20": 10_000 if seq_no >= 3 else 0,
                "a24": 11_000 if seq_no >= 5 else 0,
                "a25": 12_000 if seq_no >= 7 else 0,
            }
        )
    return {
        "version": "Q2FrameV1",
        "seq_no": seq_no,
        "logical_ts_ms": timestamp_ms,
        "phase": 1,
        "q2_updates": updates,
    }


def test_inventory_reports_frame_vs_symbol_source_time_without_calling_it_arrival_lag(
    tmp_path: Path,
):
    frame = _frame(1, "09:15:02.500")
    updates = frame["q2_updates"]
    updates[0]["ts"] = _epoch_ms("09:15:00.000")
    updates[1]["ts"] = _epoch_ms("09:15:02.500")
    updates.append(
        {
            **updates[0],
            "symbol": "000002",
            "ts": _epoch_ms("09:14:02.500"),
        }
    )
    updates.append(
        {
            **updates[0],
            "symbol": "000003",
            "ts": _epoch_ms("09:15:03.500"),
        }
    )
    updates.append({"symbol": "000004", "px": 10000})
    updates.append(dict(updates[0]))

    source = tmp_path / "q2frame-time-audit.jsonl"
    source.write_text(json.dumps(frame, separators=(",", ":")) + "\n", encoding="utf-8")

    inventory = _inventory(source, "2026-09-18")

    assert inventory["missing_update_ts_count"] == 1
    assert inventory["subsecond_update_ts_count"] == 3
    assert inventory["frame_second_mismatch_count"] == 4
    assert inventory["duplicate_symbol_occurrences_within_frame"] == 1
    assert inventory["frame_minus_source_time_ms"] == {
        "future": 1,
        "zero_to_under_3s": 3,
        "3s_to_under_60s": 0,
        "60s_or_more": 1,
        "min": -1000,
        "max": 60000,
        "meaning": "frame logical timestamp minus per-symbol source timestamp; not arrival latency",
    }


def test_q2frame_auction_engine_uses_whole_second_barriers_and_all_symbols(tmp_path: Path):
    frames = (
        _frame(1, "09:15:00.000"),
        _frame(2, "09:20:02.999"),
        _frame(3, "09:20:03.197"),
        _frame(4, "09:24:09.999"),
        _frame(5, "09:24:10.250"),
        _frame(6, "09:25:04.999"),
        _frame(7, "09:25:06.197"),
        _frame(8, "09:25:06.999"),
        _frame(9, "09:25:07.000"),
    )
    source = tmp_path / "q2frame.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
    )

    assert result["deterministic"] is True
    assert result["contract_version"] == "Task008Q2FrameAuctionEngineShadowV5"
    assert result["auction_price_field_policy"] == {
        "0920": "auction_anchor_0920_price_milli",
        "0924": "auction_anchor_0924_price_milli",
        "0925": "auction_anchor_0925_price_milli",
    }
    assert result["missing_auction_price_behavior"] == "MISSING_NO_FALLBACK_TO_LATEST_PX"
    assert result["inventory"]["symbol_count"] == 2
    assert result["ordered"]["engine_instances"] == 1
    assert result["ordered"]["input_frames_processed"] == 8
    assert result["ordered"]["input_updates_processed"] == 16
    assert result["ordered"]["first_excluded_frame"]["seq_no"] == 9
    assert result["ordered"]["first_excluded_frame"]["excluded_reason"] == (
        "AFTER_0925_FIRST_OBSERVABLE_SECOND"
    )

    anchors = result["ordered"]["anchor_evidence"]
    assert anchors["0920"]["first_observable_ms"] == _epoch_ms("09:20:03.000")
    assert anchors["0924"]["first_observable_ms"] == _epoch_ms("09:24:10.000")
    assert anchors["0925"]["first_observable_ms"] == _epoch_ms("09:25:06.000")
    for tag, clock, observed_clock in (
        ("0920", "09:20:03.000", "09:20:03.197"),
        ("0924", "09:24:10.000", "09:24:10.250"),
    ):
        revision = anchors[tag]["auction_revision"]
        assert revision["contract"] == "AuctionAnchorRevisionV3"
        assert revision["revision"] == 1
        assert revision["state"] == "READY"
        assert revision["source_observed_symbol_count"] == 2
        assert revision["source_missing_symbol_count"] == 0
        assert revision["source_coverage"] == 1.0
        assert revision["anchor_available_symbol_count"] == 2
        assert revision["missing_anchor_symbol_count"] == 0
        assert revision["anchor_coverage"] == 1.0
        assert revision["freeze_time_ms"] == _epoch_ms(clock)
        assert revision["observed_at_ms"] == _epoch_ms(observed_clock)
    assert anchors["0925"]["last_raw_update_time_ms"] == _epoch_ms("09:25:06.999")
    assert anchors["0925"]["facts_by_symbol_hash"]
    assert len(anchors["0925"]["facts_by_symbol"]) == 2
    anchor_facts = anchors["0925"]["auction_anchor_facts_by_symbol"]
    assert anchor_facts["000001"]["contract"] == "AuctionAnchorFactV1"
    assert anchor_facts["000001"]["status"] == "AVAILABLE"
    assert anchor_facts["000001"]["price_milli"] == 12_000
    assert anchor_facts["000001"]["observed_at_ms"] == _epoch_ms("09:25:06.999")
    assert anchor_facts["000001"]["historical_available_at_status"] == "UNKNOWN"
    assert anchors["0925"]["auction_anchor_facts_by_symbol_hash"]
    assert (
        anchors["0925"]["facts_by_symbol"]["000001"]["metrics"]["price_delta_milli"]
        == 1_000
    )

    revision = anchors["0925"]["auction_revision"]
    assert revision["contract"] == "AuctionAnchorRevisionV3"
    assert revision["revision"] == 1
    assert revision["state"] == "READY"
    assert revision["source_observed_symbol_count"] == 2
    assert revision["source_missing_symbol_count"] == 0
    assert revision["source_coverage"] == 1.0
    assert revision["anchor_available_symbol_count"] == 2
    assert revision["missing_anchor_symbol_count"] == 0
    assert revision["anchor_coverage"] == 1.0
    assert revision["first_observable_ms"] == _epoch_ms("09:25:06.000")
    assert revision["freeze_time_ms"] == _epoch_ms("09:25:06.000")
    assert revision["observed_at_ms"] == _epoch_ms("09:25:06.999")
    assert revision["source_layers"] == ("t1_v2_q2frame_event_time_replay",)
    assert revision["source_time_min_ms"] == _epoch_ms("09:25:06.999")
    assert revision["source_time_max_ms"] == _epoch_ms("09:25:06.999")
    assert revision["late_execution"] is False
    assert revision["content_hash"]
    assert revision["evidence_hash"]

    summary = anchors["0925"]["q2_auction_summary"]
    assert summary["status"].value == "READY"
    assert summary["input_symbol_count"] == 2
    assert summary["candidate_count"] == 2
    assert summary["metrics"]["stock_count"] == 2
    assert summary["metrics"]["auction_amount_yuan"] == 1_000_161
    assert summary["market_universe_coverage_status"] == "UNKNOWN"
    assert summary["q2_input_coverage"] is None
    assert summary["observation_time_ms"] == _epoch_ms("09:25:06.999")
    assert summary["input_content_hash"] == result["q2frame"]["sha256"]


def test_0925_observation_stays_at_last_frame_across_empty_time_gap(tmp_path: Path):
    frames = [
        _frame(1, "09:15:00.000"),
        _frame(2, "09:20:03.250"),
        _frame(3, "09:24:10.500"),
        _frame(4, "09:25:02.750"),
    ]
    for update in frames[-1]["q2_updates"]:
        update["a25"] = 12_000

    source = tmp_path / "q2frame-empty-gap-before-0925.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
    )

    anchor = result["ordered"]["anchor_evidence"]["0925"]
    observed_at_ms = _epoch_ms("09:25:02.750")
    freeze_time_ms = _epoch_ms("09:25:06.000")
    assert anchor["auction_revision"]["observed_at_ms"] == observed_at_ms
    assert anchor["auction_revision"]["evaluation_time_ms"] == freeze_time_ms
    assert anchor["auction_revision"]["freeze_time_ms"] == freeze_time_ms
    assert anchor["auction_anchor_facts_by_symbol"]["000001"]["observed_at_ms"] == observed_at_ms
    assert (
        anchor["auction_anchor_facts_by_symbol"]["000001"][
            "historical_available_at_status"
        ]
        == "UNKNOWN"
    )
    assert anchor["q2_auction_summary"]["observation_time_ms"] == observed_at_ms
    assert anchor["last_raw_frame_time_ms"] == observed_at_ms


def test_real_q2frame_shape_surfaces_partial_0925_recovery_targets(tmp_path: Path):
    frames = [_frame(seq, clock) for seq, clock in (
        (1, "09:15:00.000"),
        (2, "09:20:02.999"),
        (3, "09:20:03.197"),
        (4, "09:24:09.999"),
        (5, "09:24:10.250"),
        (6, "09:25:04.999"),
        (7, "09:25:06.197"),
        (8, "09:25:06.999"),
    )]
    for frame in frames[-2:]:
        next(update for update in frame["q2_updates"] if update["symbol"] == "000001")["a25"] = 0
    source = tmp_path / "q2frame-partial-anchor.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
    )

    revision = result["ordered"]["anchor_evidence"]["0925"]["auction_revision"]
    assert revision["state"] == "PARTIAL"
    assert revision["source_coverage"] == 1.0
    assert revision["anchor_coverage"] == 0.5
    assert revision["recovery_required"] is True
    assert revision["recovery_plan"]["contract"] == "RecoveryPlanV1"
    assert revision["recovery_plan"]["requested_symbols"] == ["000001"]
    assert revision["recovery_plan"]["missing_fields"] == ["auction_anchor_0925_price_milli"]
    assert revision["recovery_plan"]["recovery_state"] == "REQUESTED"
    assert revision["recovery_execution"] == "NOT_RUN_BY_CORE"
    facts = result["ordered"]["anchor_evidence"]["0925"]["facts_by_symbol"]
    assert facts["000001"]["metrics"]["price_delta_milli"] is None
    assert facts["000001"]["changes"]["price"] == "PRICE_UNKNOWN"
    assert facts["600000"]["metrics"]["price_delta_milli"] is not None
    anchor_facts = result["ordered"]["anchor_evidence"]["0925"][
        "auction_anchor_facts_by_symbol"
    ]
    assert anchor_facts["000001"]["status"] == "MISSING"
    assert anchor_facts["000001"]["price_milli"] is None
    assert anchor_facts["600000"]["status"] == "AVAILABLE"
    for tag in ("0920", "0924"):
        prior = result["ordered"]["anchor_evidence"][tag]["auction_revision"]
        assert prior["recovery_required"] is False
        assert prior["recovery_plan"] is None


def test_0925_anchor_fact_is_available_when_0924_anchor_and_delta_are_unknown(
    tmp_path: Path,
):
    frames = [_frame(seq, clock) for seq, clock in (
        (1, "09:15:00.000"),
        (2, "09:20:03.000"),
        (3, "09:21:00.000"),
        (4, "09:24:10.000"),
        (5, "09:24:11.000"),
        (6, "09:25:04.000"),
        (7, "09:25:06.197"),
        (8, "09:25:06.999"),
    )]
    # The 0924 anchor is unavailable at its own barrier and remains unavailable
    # in subsequent Q2 snapshots, while 0925 is independently captured.
    for frame in frames:
        if frame["seq_no"] >= 5:
            for update in frame["q2_updates"]:
                update["a24"] = 0
    source = tmp_path / "q2frame-0925-standalone-anchor.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
    )
    anchors = result["ordered"]["anchor_evidence"]
    fact = anchors["0925"]["auction_anchor_facts_by_symbol"]["000001"]
    adjacent = anchors["0925"]["facts_by_symbol"]["000001"]

    assert anchors["0925"]["fact_status_scope"] == "adjacent_auction_comparison"
    assert anchors["0925"]["anchor_fact_status_scope"] == "standalone_current_anchor"
    assert anchors["0925"]["auction_revision"]["prior_deltas"]["0924"] == "UNKNOWN"
    assert fact["status"] == "AVAILABLE"
    assert fact["price_milli"] == 12_000
    assert fact["historical_available_at_status"] == "UNKNOWN"
    assert adjacent["metrics"]["price_delta_milli"] is None
    assert adjacent["changes"]["price"] == "PRICE_UNKNOWN"


def test_q2frame_session_reaches_opening_with_one_engine_and_symbol_source_times(tmp_path: Path):
    frames = [
        _frame(1, "09:15:00.000"),
        _frame(2, "09:20:02.999"),
        _frame(3, "09:20:03.197"),
        _frame(4, "09:24:09.999"),
        _frame(5, "09:24:10.250"),
        _frame(6, "09:25:04.999"),
        _frame(7, "09:25:06.197"),
        _frame(8, "09:25:06.999"),
        _frame(9, "09:25:07.000"),
        _frame(10, "09:32:10.197"),
        _frame(11, "09:32:10.999"),
        _frame(12, "09:32:11.000"),
    ]
    # Source timestamps are per symbol even when they arrive in one Q2Frame.
    frames[9]["q2_updates"][0]["ts"] = _epoch_ms("09:32:10.197")
    frames[9]["q2_updates"][1]["ts"] = _epoch_ms("09:32:09.999")
    frames[10]["q2_updates"][0]["ts"] = _epoch_ms("09:32:10.999")
    frames[10]["q2_updates"][1]["ts"] = _epoch_ms("09:32:10.250")

    source = tmp_path / "q2frame-opening.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
        include_opening=True,
    )

    assert result["contract_version"] == "Task008Q2FrameSessionEngineShadowV7"
    assert result["deterministic"] is True
    assert result["ordered"]["engine_instances"] == 1
    assert result["ordered"]["input_frames_processed"] == 11
    assert result["ordered"]["first_excluded_frame"]["seq_no"] == 12
    assert result["ordered"]["first_excluded_frame"]["excluded_reason"] == (
        "AFTER_OPENING_0932_EVALUATION_SECOND"
    )
    opening = result["ordered"]["opening_evidence"]["OPENING_0932"]
    assert opening["evaluation_time_ms"] == _epoch_ms("09:32:10.000")
    assert opening["input_frames_included"] == 11
    cross_section = opening["cross_section_facts"]
    assert cross_section["contract"] == "CrossSectionFactsV1"
    assert cross_section["scope"] == "OBSERVED_COHORT"
    assert cross_section["scope_authority"] == (
        "Q2FRAME_INPUT_COHORT_ONLY_NOT_FULL_MARKET"
    )
    assert cross_section["expected_count"] == 2
    assert cross_section["observed_count"] == 2
    assert cross_section["missing_count"] == 0
    assert cross_section["coverage"] == 1.0
    assert cross_section["field_denominators"] == {
        "price_milli": 2,
        "pre_close_milli": 2,
    }
    assert cross_section["market_breadth"] == {
        "up_count": 1,
        "down_count": 0,
        "flat_count": 1,
        "unknown_count": 0,
    }
    assert cross_section["source_layers"] == [
        "t1_v2_q2frame_event_time_replay"
    ]
    assert cross_section["content_hash"]
    assert cross_section["fact_only"] is True
    assert opening["fact_status_counts"] == {"READY": 2}
    transition = opening["opening_transition_summary"]
    assert transition["contract"] == "OpeningTransitionSummaryV1"
    assert transition["baseline"]["tag"] == "0925"
    assert transition["baseline"]["freeze_time_ms"] == _epoch_ms("09:25:06.000")
    assert transition["opening_evaluation_time_ms"] == _epoch_ms("09:32:10.000")
    assert transition["historical_available_at"] == "UNKNOWN_NOT_INFERRED"
    assert transition["rabbit_arrival_order"] == "UNKNOWN_NOT_INFERRED"
    assert transition["facts"]["status"] == "READY"
    assert transition["facts"]["transition_comparable_count"] == 2
    first_transition = transition["facts"]["facts_by_symbol"]["000001"]
    assert first_transition["auction_change_pct"] == pytest.approx(20.0)
    assert first_transition["opening_change_pct"] == pytest.approx(0.0)
    assert first_transition["delta_change_pct"] == pytest.approx(-20.0)
    assert first_transition["auction_source_time_ms"] == _epoch_ms("09:25:06.999")
    assert first_transition["opening_source_time_ms"] == _epoch_ms("09:32:10.999")
    assert opening["facts_by_symbol"]["000001"]["timestamp_ms"] == _epoch_ms(
        "09:32:10.999"
    )
    assert opening["facts_by_symbol"]["600000"]["timestamp_ms"] == _epoch_ms(
        "09:32:10.250"
    )
    assert opening["fact_status_scope"] == "change_pct_and_source_time"
    assert opening["opening_fact_field_status_counts"] == {
        "amount_2m_yuan": {"AVAILABLE": 2},
        "change_pct": {"AVAILABLE": 2},
        "limit_state": {"AVAILABLE": 2},
        "speed_1m": {"UNAVAILABLE": 2},
    }
    assert opening["opening_fact_field_status_hash"]
    amount_summary = opening["amount_2m_summary"]
    assert amount_summary["contract"] == "OpeningAmountSummaryV1"
    assert amount_summary["scope"] == "OBSERVED_COHORT"
    assert amount_summary["full_market_coverage"] == "UNPROVEN"
    assert amount_summary["amount_2m_yuan_total_count"] == 2
    assert amount_summary["amount_2m_yuan_present_count"] == 2
    assert amount_summary["amount_2m_yuan_sum"] == 22_201
    assert amount_summary["amount_2m_yuan_status"] == "available"
    limit_summary = opening["limit_state_summary"]
    assert limit_summary["contract"] == "OpeningLimitStateSummaryV1"
    assert limit_summary["scope"] == "OBSERVED_COHORT"
    assert limit_summary["full_market_coverage"] == "UNPROVEN"
    assert limit_summary["expected_count"] == 2
    assert limit_summary["observed_count"] == 2
    assert limit_summary["limit_state_valid_count"] == 2
    assert limit_summary["limit_state_counts"] == {
        "up_count": 0,
        "normal_count": 2,
        "down_count": 0,
    }
    assert limit_summary["content_hash"]


def test_q2frame_opening_emits_partial_facts_instead_of_stopping_on_stale_symbol(tmp_path: Path):
    frames = [_frame(1, "09:15:00.000")]
    for seq_no, clock in enumerate(
        (
            "09:20:03.000",
            "09:24:10.000",
            "09:25:06.000",
            "09:30:00.000",
            "09:32:10.000",
        ),
        start=2,
    ):
        frame = _frame(seq_no, clock)
        frame["q2_updates"] = [
            update for update in frame["q2_updates"] if update["symbol"] == "000001"
        ]
        frames.append(frame)
    source = tmp_path / "q2frame-opening-partial.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
        include_opening=True,
    )

    opening = result["ordered"]["opening_evidence"]["OPENING_0932"]
    assert opening["fact_status_counts"] == {"PARTIAL": 1, "READY": 1}
    assert opening["stale_symbol_count"] == 1
    assert opening["expected_symbol_count"] == 2
    # The older quote is retained as an observed fact and is identified by the
    # separate stale count; aggregation does not silently discard it.
    assert opening["limit_state_summary"]["observed_count"] == 2
    assert opening["limit_state_summary"]["missing_symbol_count"] == 0
    assert opening["limit_state_summary"]["scope"] == "OBSERVED_COHORT"
    assert result["ordered"]["engine_instances"] == 1


def test_q2frame_opening_aggregates_auxiliary_field_quality_without_stopping(tmp_path: Path):
    frames = [
        _frame(1, "09:15:00.000"),
        _frame(2, "09:20:03.000"),
        _frame(3, "09:24:10.000"),
        _frame(4, "09:25:06.000"),
        _frame(5, "09:32:10.000"),
    ]
    for frame in frames:
        frame["q2_updates"] = [
            update for update in frame["q2_updates"] if update["symbol"] == "000001"
        ]
    frames[-1]["q2_updates"][0]["amt2m"] = "bad"
    frames[-1]["q2_updates"][0]["ls"] = 7
    frames[-1]["q2_updates"][0]["spd1m"] = "bad"

    source = tmp_path / "q2frame-opening-field-quality.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
        include_opening=True,
    )

    opening = result["ordered"]["opening_evidence"]["OPENING_0932"]
    assert opening["fact_status_counts"] == {"READY": 1}
    assert opening["opening_fact_field_status_by_symbol"]["000001"] == {
        "change_pct": "AVAILABLE",
        "amount_2m_yuan": "INVALID",
        "limit_state": "INVALID",
        "speed_1m": "INVALID",
    }
    assert opening["opening_fact_field_status_counts"] == {
        "amount_2m_yuan": {"INVALID": 1},
        "change_pct": {"AVAILABLE": 1},
        "limit_state": {"INVALID": 1},
        "speed_1m": {"INVALID": 1},
    }
    limit_summary = opening["limit_state_summary"]
    assert limit_summary["limit_state_total_count"] == 1
    assert limit_summary["limit_state_present_count"] == 1
    assert limit_summary["limit_state_valid_count"] == 0
    assert limit_summary["limit_state_invalid_count"] == 1
    assert limit_summary["cohort_field_status"] == "unavailable"
    assert limit_summary["full_market_coverage"] == "UNPROVEN"
    assert result["ordered"]["engine_instances"] == 1


def test_q2frame_opening_report_includes_explicit_plate_context_deterministically(
    tmp_path: Path,
):
    frames = (
        _frame(1, "09:15:00.000"),
        _frame(2, "09:20:03.000"),
        _frame(3, "09:24:10.000"),
        _frame(4, "09:25:06.000"),
        _frame(5, "09:32:09.000"),
    )
    source = tmp_path / "q2frame-opening-with-plate-context.jsonl"
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )
    context = build_opening_plate_amount_context(
        trade_date="2026-09-18",
        source_provenance={
            "mapping_snapshot_sha256": "a" * 64,
            "auction_rows_sha256": "b" * 64,
        },
        mapped_symbols_by_plate={"AI": ("000001", "600000")},
        auction_symbols_by_plate={"AI": ("000001", "600000")},
        auction_top1_amount_ratio_by_plate={"AI": 0.5},
        selected_plates=("AI",),
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-18",
        include_opening=True,
        plate_amount_context=context,
    )

    opening = result["ordered"]["opening_evidence"]["OPENING_0932"]
    repeated = result["repeat"]["opening_evidence"]["OPENING_0932"]
    plate = opening["plate_amount_summary"]["plates"][0]
    assert result["deterministic"] is True
    assert result["contract_version"] == "Task008Q2FrameSessionEngineShadowV8"
    assert opening["plate_amount_context"]["content_hash"] == context["content_hash"]
    assert plate["plate"] == "AI"
    assert plate["open_window_amount_yuan"] == 21_001
    assert plate["open_valid_count"] == 2
    assert plate["comparison_valid_count"] == 2
    assert opening["plate_amount_summary"] == repeated["plate_amount_summary"]


def test_q2frame_rejects_plate_context_without_opening_barrier(tmp_path: Path):
    source = tmp_path / "q2frame-auction-only.jsonl"
    source.write_text(
        json.dumps(_frame(1, "09:15:00.000"), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    context = build_opening_plate_amount_context(
        trade_date="2026-09-18",
        source_provenance={},
        mapped_symbols_by_plate={},
        auction_symbols_by_plate={},
        auction_top1_amount_ratio_by_plate={},
        selected_plates=(),
    )
    import pytest

    with pytest.raises(ValueError, match="include_opening"):
        run_q2frame_auction_engine_shadow(
            q2frame_path=source,
            trade_date="2026-09-18",
            plate_amount_context=context,
        )
