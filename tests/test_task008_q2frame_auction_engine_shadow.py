from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from examples.run_task008_q2frame_auction_engine_shadow import (
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
            }
        )
    return {
        "version": "Q2FrameV1",
        "seq_no": seq_no,
        "logical_ts_ms": timestamp_ms,
        "phase": 1,
        "q2_updates": updates,
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
