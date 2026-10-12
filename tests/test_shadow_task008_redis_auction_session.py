import json
from pathlib import Path

import pytest

from examples.shadow_task008_redis_auction_session import (
    ReadOnlyQ2Client,
    run_shadow,
)
from engine_core import AuctionAnchorSessionV1


class FakeClient:
    def smembers(self, key):
        return {"q2:active:20261009": ["000001", "000002"]}.get(key, [])

    def hgetall(self, key):
        symbol = key.removeprefix("q2:")
        rows = {
            "000001": {"px": "1000", "pc": "990", "amt": "100", "ts": "1791509106000", "a20": "990", "a24": "995", "a25": "1000"},
            "000002": {"px": "2000", "pc": "1990", "amt": "200", "ts": "1791509106000", "a20": "1990", "a24": "1995", "a25": "2000"},
        }
        return rows.get(symbol, {})

    def close(self):
        pass


def test_read_only_client_only_exposes_projection_reads():
    fake = FakeClient()
    readonly = ReadOnlyQ2Client(fake)
    assert readonly.smembers("q2:active:20261009") == ["000001", "000002"]
    assert readonly.hgetall("q2:000001")["a20"] == "990"
    for name in ("set", "hset", "delete", "pipeline"):
        with pytest.raises(RuntimeError):
            getattr(readonly, name)


def test_synthetic_capture_emits_three_anchor_events(tmp_path: Path):
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "read_completed_at": "2026-10-09T09:25:06+08:00",
        "reads": [
            {"operation": "smembers", "key": "q2:active:20261009", "value": ["000001", "000002"]},
            {"operation": "hgetall", "key": "q2:000001", "value": {"px": "1000", "pc": "990", "amt": "100", "ts": "1791509106000", "a20": "990", "a24": "995", "a25": "1000"}},
            {"operation": "hgetall", "key": "q2:000002", "value": {"px": "2000", "pc": "1990", "amt": "200", "ts": "1791509106000", "a20": "1990", "a24": "1995", "a25": "2000"}},
        ],
    }
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    result_dir = tmp_path / "result"
    run_shadow(source="capture", capture_path=capture_path, out_dir=result_dir)
    events = [json.loads(line) for line in (result_dir / "shadow_events.jsonl").read_text().splitlines()]
    assert [event["tag"] for event in events] == ["0920", "0924", "0925"]
    assert [event["available_count"] for event in events] == [2, 2, 2]


def test_live_requires_confirmation_and_redacts_client_errors(tmp_path: Path):
    class FailingClient(FakeClient):
        def hgetall(self, key):
            raise ValueError("secret-host-123")

    with pytest.raises(RuntimeError):
        run_shadow(
            source="live", live_client=object(), out_dir=tmp_path / "unconfirmed",
            trade_date="2026-10-09",
        )

    with pytest.raises(RuntimeError) as exc_info:
        run_shadow(
            source="live", live_client=FailingClient(), out_dir=tmp_path / "redacted",
            trade_date="2026-10-09", confirm_read_only=True,
        )
    assert "secret-host-123" not in str(exc_info.value)


def test_live_redacts_session_errors(monkeypatch, tmp_path: Path):
    def fail_observe(*args, **kwargs):
        raise ValueError("secret-host-123")

    monkeypatch.setattr(AuctionAnchorSessionV1, "observe_late_start", fail_observe)
    with pytest.raises(RuntimeError) as exc_info:
        run_shadow(
            source="live", live_client=FakeClient(), out_dir=tmp_path / "session-error",
            trade_date="2026-10-09", confirm_read_only=True,
        )

    assert "secret-host-123" not in str(exc_info.value)
    assert "secret-host-123" not in str(exc_info.value.__cause__ or "")
    assert exc_info.value.__suppress_context__ is True
