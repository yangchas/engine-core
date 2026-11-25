import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from engine_core.guardian import gates as guardian_gates
from engine_core import report_mailer


HANDLER_PATH = Path(__file__).parents[1] / "examples" / "guardian_handler.py"
SPEC = importlib.util.spec_from_file_location("guardian_handler_clock", HANDLER_PATH)
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)


def _stub_gate_input(monkeypatch, payload):
    seen = []

    def gate(event, now, state):
        seen.append(now)
        return {"status": "ready"}

    monkeypatch.setattr(
        guardian_gates, "default_gates", lambda: {"snapshot_0925_ready": gate}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert handler.main(["gate", "snapshot_0925_ready"]) == 0
    return seen[0]


@pytest.mark.parametrize(
    ("raw_now", "expected"),
    [
        ("2026-10-23T09:25:10+08:00", "2026-10-23T09:25:10+08:00"),
        ("2026-10-23T09:25:10", "2026-10-23T09:25:10+08:00"),
    ],
)
def test_gate_uses_request_now_with_timezone(monkeypatch, raw_now, expected):
    actual = _stub_gate_input(
        monkeypatch, {"event": "auction_0925_check", "now": raw_now}
    )

    assert actual.isoformat() == expected


@pytest.mark.parametrize("payload", [{"event": "auction_0925_check"},
                                      {"event": "auction_0925_check", "now": "invalid"}])
def test_gate_falls_back_to_host_clock_when_request_now_is_unusable(
    monkeypatch, payload
):
    host_now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    class FixedDateTime:
        @classmethod
        def now(cls):
            return host_now

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(handler, "datetime", FixedDateTime)
    actual = _stub_gate_input(monkeypatch, payload)

    assert actual == host_now.astimezone()


@pytest.mark.parametrize(
    ("payload", "expected_day"),
    [
        ({"request": {"date": "2026-10-20"}, "trade_date": "2026-10-22",
          "now": "2026-10-23T09:25:10+08:00"}, "2026-10-20"),
        ({"request": {"event": "auction_0925_check"}, "trade_date": "2026-10-22",
          "now": "2026-10-23T09:25:10+08:00"}, "2026-10-22"),
        ({"request": {"event": "auction_0925_check"},
          "now": "2026-10-23T09:25:10+08:00"}, "2026-10-23"),
        ({"request": {"event": "auction_0925_check"}, "now": "invalid"},
         "2026-10-25"),
    ],
)
def test_alert_date_fallback_prefers_explicit_dates_then_request_now(
    monkeypatch, payload, expected_day
):
    host_now = datetime(2026, 10, 25)
    built_days = []

    class FixedDateTime:
        @classmethod
        def now(cls):
            return host_now

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    class Message:
        def set_content(self, content):
            self.content = content

        def __getitem__(self, name):
            return "subject"

    def build_message(day, *args):
        built_days.append(day)
        return Message()

    monkeypatch.setattr(handler, "datetime", FixedDateTime)
    monkeypatch.setattr(report_mailer, "build_alert_message", build_message)
    monkeypatch.setattr(
        report_mailer, "send_message", lambda *args, **kwargs: SimpleNamespace(status="dry_run")
    )

    assert handler._alert(payload, dry_run=True) == 0
    assert built_days == [expected_day]
