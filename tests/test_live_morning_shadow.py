from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from engine_core import DataStatus, TimerFiring, build_a_share_session_plan, build_calendar_snapshot, local_datetime_ms
from examples import run_live_morning_shadow as live


TZ = ZoneInfo("Asia/Shanghai")
CALENDAR = build_calendar_snapshot(
    ["2026-09-14"],
    calendar_id="test-calendar",
    version="test-calendar-v1",
    timezone_name="Asia/Shanghai",
    declared_valid_from="2026-01-01",
    declared_valid_to="2026-12-31",
    source_guard_valid_from="2025-12-01",
    source_guard_valid_to="2027-01-31",
)


def _dt(text: str) -> datetime:
    return datetime.fromisoformat("2026-09-14T" + text).replace(tzinfo=TZ)


def _firing(timer_id: str) -> TimerFiring:
    plan = build_a_share_session_plan("2026-09-14", CALENDAR)
    spec = next(item for item in live.CORE_TIMER_SPECS if item.timer_id == timer_id)
    scheduled = spec.scheduled_time_ms(plan)
    return TimerFiring(
        timer_id=timer_id,
        scheduled_time_ms=scheduled,
        fired_time_ms=scheduled,
        trigger_basis=spec.trigger_basis,
        origin="NORMAL",
        late_by_ms=0,
        session_plan_hash=plan.content_hash,
    )


def test_build_node_evidence_keeps_business_and_observation_times_separate():
    firing = _firing("AUCTION_0926")
    rows = {
        "600519": [
            {
                "auction_tag": "0924",
                "symbol": "600519",
                "trade_date": "20260914",
                "ts": _dt("09:24:10"),
                "px_milli": 1276000,
                "chg_bp": 6,
                "match_amt_yuan": 100,
                "rest_bid_amt_yuan": 200,
                "rest_ask_amt_yuan": 50,
            },
            {
                "auction_tag": "0925",
                "symbol": "600519",
                "trade_date": "20260914",
                "ts": _dt("09:25:06"),
                "px_milli": 1277000,
                "chg_bp": 7,
                "match_amt_yuan": 150,
                "rest_bid_amt_yuan": 250,
                "rest_ask_amt_yuan": 40,
            },
        ]
    }
    result = live.build_node_evidence(
        firing,
        observed_at=_dt("09:26:00"),
        trade_date="2026-09-14",
        symbols=("600519",),
        td_rows_by_symbol=rows,
    )
    assert result["business_anchor_time"] == firing.scheduled_time_ms
    assert result["observed_at_ms"] == local_datetime_ms("2026-09-14", "09:26:00", timezone_name="Asia/Shanghai")
    assert result["fact_dispatch"][0]["facts"][0]["timer_id"] == "AUCTION_0926"
    assert len(result["input_sha256"]) == 64
    assert result["semantic_hash"]


def test_build_node_evidence_normalizes_q2_status_for_trace():
    quote = SimpleNamespace(
        source_record_time_ms=local_datetime_ms("2026-09-14", "09:32:00", timezone_name="Asia/Shanghai"),
        price_milli=1276000,
        pre_close_milli=1270000,
        amount_2m_yuan=100,
        limit_state=0,
        name="fixture",
        speed_1m_bp=10,
    )
    projection = SimpleNamespace(
        status=DataStatus.STALE,
        consistency_status="BEST_EFFORT_STALE",
        coverage=1.0,
        quotes={"600519": quote},
        expected_symbols=("600519",),
        oldest_source_time_ms=1,
        newest_source_time_ms=2,
        content_hash="q2-hash",
    )
    result = live.build_node_evidence(
        _firing("OPENING_0932"),
        observed_at=_dt("09:32:00"),
        trade_date="2026-09-14",
        symbols=("600519",),
        td_rows_by_symbol={
            "600519": [
                {
                    "auction_tag": "0925",
                    "symbol": "600519",
                    "trade_date": "20260914",
                    "ts": _dt("09:25:06"),
                    "px_milli": 1276000,
                    "chg_bp": 6,
                    "match_amt_yuan": 100,
                    "rest_bid_amt_yuan": 200,
                    "rest_ask_amt_yuan": 50,
                }
            ]
        },
        projection=projection,
    )
    assert result["q2"]["status"] == "STALE"
    assert result["q2"]["content_hash"] == "q2-hash"


def test_live_shell_captures_each_node_at_its_due_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock_values = iter((_dt("09:15:00"), _dt("09:15:00"), _dt("09:26:00"), _dt("09:32:00")))
    captured: list[tuple[str, datetime]] = []

    monkeypatch.setattr(live, "_startup_evidence", lambda **kwargs: {"read_only": True})

    def fake_capture(firing, **kwargs):
        observed = kwargs["observed_at"]
        captured.append((firing.timer_id, observed))
        return {"timer": live._timer_payload(firing), "observed_at": observed.isoformat()}

    monkeypatch.setattr(live, "_capture_node", fake_capture)
    manifest = live.run_live_morning_shadow(
        trade_date="2026-09-14",
        calendar=CALENDAR,
        output_dir=tmp_path / "run",
        symbols=("600519",),
        stale_after_ms=60_000,
        td_config={},
        now_fn=lambda: next(clock_values),
        sleep_fn=lambda _: None,
        poll_seconds=0,
    )
    assert [item[0] for item in captured] == ["AUCTION_0926", "OPENING_0932"]
    assert [item[1].strftime("%H:%M:%S") for item in captured] == ["09:26:00", "09:32:00"]
    assert manifest["node_timer_ids"] == ("AUCTION_0926", "OPENING_0932")
    assert (tmp_path / "run" / "startup.json").exists()
    assert (tmp_path / "run" / "AUCTION_0926.json").exists()
    assert (tmp_path / "run" / "OPENING_0932.json").exists()
    assert (tmp_path / "run" / "manifest.json").exists()
    manifest_payload = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_payload["build_identity"]["kind"] == "SOURCE_TREE_SHA256"
    assert len(manifest_payload["build_identity"]["value"]) == 64


def test_runtime_build_identity_accepts_explicit_immutable_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGINE_CORE_BUILD_ID", "release-20260916-1")
    assert live._runtime_build_identity() == {
        "kind": "EXPLICIT",
        "value": "release-20260916-1",
    }


def test_late_start_marks_recovery_and_does_not_reuse_normal_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock_values = iter((_dt("09:30:00"), _dt("09:30:00"), _dt("09:32:00")))
    origins: list[str] = []
    monkeypatch.setattr(live, "_startup_evidence", lambda **kwargs: {"read_only": True})

    def fake_capture(firing, **kwargs):
        origins.append(firing.origin)
        return {"timer": live._timer_payload(firing)}

    monkeypatch.setattr(live, "_capture_node", fake_capture)
    manifest = live.run_live_morning_shadow(
        trade_date="2026-09-14",
        calendar=CALENDAR,
        output_dir=tmp_path / "late-run",
        symbols=("600519",),
        stale_after_ms=60_000,
        td_config={},
        now_fn=lambda: next(clock_values),
        sleep_fn=lambda _: None,
        poll_seconds=0,
    )
    assert origins == ["RECOVERY_CATCHUP", "RECOVERY_CATCHUP"]
    assert manifest["origin"] == "RECOVERY_CATCHUP"


def test_live_shell_rejects_clock_date_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "_startup_evidence", lambda **kwargs: {"read_only": True})
    with pytest.raises(ValueError, match="clock date"):
        live.run_live_morning_shadow(
            trade_date="2026-09-14",
            calendar=CALENDAR,
            output_dir=tmp_path / "bad-date",
            symbols=("600519",),
            stale_after_ms=60_000,
            td_config={},
            now_fn=lambda: datetime(2026, 9, 15, 9, 15, tzinfo=TZ),
            sleep_fn=lambda _: None,
            poll_seconds=0,
        )
