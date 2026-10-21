"""Trade-day context against the real Core trading calendar (read-only copy under tmp_path for writes)."""
import json
import shutil
from pathlib import Path

import pytest

from engine_core import trade_day_context as ctx

REAL = Path.home() / "core-data"


@pytest.fixture()
def root(tmp_path):
    if not (REAL / "trade_calendar" / "calendar.json").is_file():
        pytest.skip("real Core trade calendar not available")
    shutil.copytree(REAL / "trade_calendar", tmp_path / "trade_calendar", ignore=shutil.ignore_patterns("day_context.json"))
    return tmp_path


def test_wednesday_neighbours_match_the_real_calendar(root):
    c = ctx.build_context(root, "2026-10-21")
    assert (c["today"], c["today_is_trading_day"]) == ("2026-10-21", True)
    assert c["yesterday"] == {"date": "2026-10-20", "is_trading_day": True}
    assert c["tomorrow"] == {"date": "2026-10-22", "is_trading_day": True}
    assert (c["prev_trade_date"], c["prev2_trade_date"]) == ("2026-10-20", "2026-10-19")
    assert (c["next_trade_date"], c["next2_trade_date"]) == ("2026-10-22", "2026-10-23")


def test_monday_previous_trade_day_skips_the_weekend_but_yesterday_is_flagged(root):
    c = ctx.build_context(root, "2026-10-19")
    assert c["yesterday"] == {"date": "2026-10-18", "is_trading_day": False}
    assert c["prev_trade_date"] == "2026-10-16" and c["prev2_trade_date"] == "2026-10-15"


def test_after_the_national_day_holiday_previous_is_the_last_session_before_it(root):
    assert ctx.build_context(root, "2026-10-08")["prev_trade_date"] == "2026-09-30"
    assert ctx.previous_trade_date(root, "2026-10-09") == "2026-10-08"


def test_refresh_persists_and_load_reuses_only_a_matching_day_and_calendar_version(root):
    written = ctx.refresh_context(root, "2026-10-21")
    path = root / "trade_calendar" / ctx.FILENAME
    assert json.loads(path.read_text())["prev_trade_date"] == "2026-10-20"
    assert ctx.load_context(root, "2026-10-21")["generated_at"] == written["generated_at"]
    other = ctx.load_context(root, "2026-10-22")           # different day: recomputed, never the stored one
    assert other["as_of_day"] == "2026-10-22" and other["prev_trade_date"] == "2026-10-21"
    stored = json.loads(path.read_text())
    stored["calendar_version"] = "older-version"
    path.write_text(json.dumps(stored))
    assert ctx.load_context(root, "2026-10-21")["generated_at"] != written["generated_at"]


def test_check_context_detects_a_tampered_file(root):
    assert ctx.check_context(root, "2026-10-21")["status"] == "MISSING"
    ctx.refresh_context(root, "2026-10-21")
    assert ctx.check_context(root, "2026-10-21")["status"] == "OK"
    path = root / "trade_calendar" / ctx.FILENAME
    stored = json.loads(path.read_text())
    stored["prev_trade_date"] = "2026-10-19"
    path.write_text(json.dumps(stored))
    assert ctx.check_context(root, "2026-10-21") ["differing"] == ["prev_trade_date"]


def test_no_module_derives_the_previous_trade_day_outside_the_context():
    """Guard: previous/next trade day questions go through trade_day_context (the calendar infrastructure)."""
    import re
    base = Path(__file__).resolve().parents[1]
    allowed = {"src/engine_core/trade_day_context.py", "src/engine_core/data_pipeline/trade_calendar.py",
               "src/engine_core/data_pipeline/ops.py", "src/engine_core/prep_trade_day.py"}
    offenders = []
    for folder in ("src/engine_core", "examples"):
        for path in (base / folder).rglob("*.py"):
            rel = str(path.relative_to(base))
            if rel in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"(TradeCalendarStore\([^)]*\)|store|calendar)\.(previous|next)_trade_date\(", text):
                offenders.append(rel)
    assert offenders == []
