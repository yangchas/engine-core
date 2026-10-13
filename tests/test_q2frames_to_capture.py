import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from examples.q2frames_to_capture import convert_q2frames_to_capture
from examples.shadow_task008_redis_auction_session import run_shadow


def _ms(date: str, time: str) -> int:
    return int(datetime.fromisoformat(f"{date}T{time}").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def _frame(seq: int, time: str, updates: list[dict]) -> dict:
    return {
        "version": "Q2FrameV1",
        "seq_no": seq,
        "logical_ts_ms": _ms("2026-10-09", time),
        "q2_updates": updates,
    }


def test_conversion_merges_in_order_and_capture_runs_three_anchor_events(tmp_path: Path):
    source = tmp_path / "frames.jsonl"
    frames = [
        _frame(1, "09:20:03", [
            {"symbol": "000002", "px": 2000, "pc": 1990, "a20": 1990},
            {"symbol": "000001", "px": 1000, "pc": 990, "a20": 990},
        ]),
        _frame(2, "09:24:10", [
            {"symbol": "000001", "px": 1001, "a24": 995},
            {"symbol": "000002", "a24": 1995},
        ]),
        _frame(3, "09:25:06", [
            {"symbol": "000001", "px": 1002, "amt": 100, "ts": _ms("2026-10-09", "09:25:06"), "a25": 1000},
            {"symbol": "000002", "amt": 200, "ts": _ms("2026-10-09", "09:25:06"), "a25": 2000},
        ]),
    ]
    source.write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")
    output = tmp_path / "capture.json"

    capture = convert_q2frames_to_capture(source, "2026-10-09", "09:25:06", output)

    assert capture["contract_version"] == "RedisQ2ReadCaptureV1"
    assert capture["read_completed_at"] == "2026-10-09T09:25:06+08:00"
    assert capture["reads"][0] == {
        "operation": "smembers", "key": "q2:active:20261009", "value": ["000001", "000002"]
    }
    rows = {read["key"]: read["value"] for read in capture["reads"][1:]}
    assert rows["q2:000001"] == {
        "px": "1002", "pc": "990", "a20": "990", "a24": "995",
        "amt": "100", "ts": str(_ms("2026-10-09", "09:25:06")), "a25": "1000",
    }
    assert "symbol" not in rows["q2:000001"]
    run_shadow(source="capture", capture_path=output, out_dir=tmp_path / "shadow")
    events = [json.loads(line) for line in (tmp_path / "shadow" / "shadow_events.jsonl").read_text().splitlines()]
    assert [event["tag"] for event in events] == ["0920", "0924", "0925"]


def test_conversion_stops_before_first_frame_after_cutoff(tmp_path: Path):
    source = tmp_path / "frames.jsonl"
    frames = [
        _frame(1, "09:24:10", [{"symbol": "000001", "px": 1000, "pc": 990, "a24": 995}]),
        _frame(2, "09:25:06", [{"symbol": "000001", "a25": 1000}]),
    ]
    source.write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")

    capture = convert_q2frames_to_capture(source, "2026-10-09", "09:24:10", tmp_path / "capture.json")

    assert [read["operation"] for read in capture["reads"]] == ["smembers", "hgetall"]
    assert capture["reads"][1]["value"] == {"px": "1000", "pc": "990", "a24": "995"}


def test_conversion_refuses_existing_output(tmp_path: Path):
    source = tmp_path / "frames.jsonl"
    source.write_text("", encoding="utf-8")
    output = tmp_path / "capture.json"
    output.write_text("sealed", encoding="utf-8")

    with pytest.raises(FileExistsError):
        convert_q2frames_to_capture(source, "2026-10-09", "09:25:06", output)
