from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    assert result["contract_version"] == "Task008Q2FrameAuctionEngineShadowV3"
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
    for tag, clock in (("0920", "09:20:03.000"), ("0924", "09:24:10.000")):
        revision = anchors[tag]["auction_revision"]
        assert revision["contract"] == "AuctionAnchorRevisionV1"
        assert revision["revision"] == 1
        assert revision["state"] == "READY"
        assert revision["freeze_time_ms"] == _epoch_ms(clock)
        assert revision["observed_at_ms"] is None
    assert anchors["0925"]["last_raw_update_time_ms"] == _epoch_ms("09:25:06.999")
    assert anchors["0925"]["facts_by_symbol_hash"]
    assert len(anchors["0925"]["facts_by_symbol"]) == 2
    assert (
        anchors["0925"]["facts_by_symbol"]["000001"]["metrics"]["price_delta_milli"]
        == 1_000
    )

    revision = anchors["0925"]["auction_revision"]
    assert revision["contract"] == "AuctionAnchorRevisionV1"
    assert revision["revision"] == 1
    assert revision["state"] == "READY"
    assert revision["first_observable_ms"] == _epoch_ms("09:25:06.000")
    assert revision["freeze_time_ms"] == _epoch_ms("09:25:06.000")
    assert revision["observed_at_ms"] is None
    assert revision["source_layers"] == ("t1_v2_q2frame_event_time_replay",)
    assert revision["source_time_min_ms"] == _epoch_ms("09:25:06.999")
    assert revision["source_time_max_ms"] == _epoch_ms("09:25:06.999")
    assert revision["late_execution"] is False
    assert revision["content_hash"]
    assert revision["evidence_hash"]


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

    assert result["contract_version"] == "Task008Q2FrameSessionEngineShadowV2"
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
    assert opening["fact_status_counts"] == {"READY": 2}
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
        "speed_1m": {"UNKNOWN_UNIT_MAPPING": 2},
    }
    assert opening["opening_fact_field_status_hash"]


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
        "speed_1m": "UNKNOWN_UNIT_MAPPING",
    }
    assert opening["opening_fact_field_status_counts"] == {
        "amount_2m_yuan": {"INVALID": 1},
        "change_pct": {"AVAILABLE": 1},
        "limit_state": {"INVALID": 1},
        "speed_1m": {"UNKNOWN_UNIT_MAPPING": 1},
    }
    assert result["ordered"]["engine_instances"] == 1
