# DATA: real:~/core-data/trade_calendar (copied read-only into tmp_path; skipped when absent)
"""Trade-day context against the real Core trading calendar; the context is computed, never persisted (design/31 B9)."""
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


def _files(root):
    return sorted(path.name for path in (root / "trade_calendar").iterdir())


def test_refresh_returns_the_context_and_writes_no_file(root):
    before = _files(root)
    context = ctx.refresh_context(root, "2026-10-21")
    assert context["prev_trade_date"] == "2026-10-20" and context["next_trade_date"] == "2026-10-22"
    assert _files(root) == before and "day_context.json" not in _files(root)


def test_load_context_is_computed_from_the_calendar_for_the_asked_day_and_leaves_no_file(root):
    before = _files(root)
    loaded = ctx.load_context(root, "2026-10-22")
    fresh = ctx.build_context(root, "2026-10-22")
    assert {k: v for k, v in loaded.items() if k != "generated_at"} == {k: v for k, v in fresh.items() if k != "generated_at"}
    assert loaded["as_of_day"] == "2026-10-22" and loaded["prev_trade_date"] == "2026-10-21"
    assert _files(root) == before


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


def test_directional_queries_at_real_calendar_boundaries(root):
    from engine_core.calendar import CalendarCoverageError
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    store = TradeCalendarStore(root)
    days = store.all_trading_dates()
    first, last = days[0], days[-1]
    assert ctx.previous_trade_date(root, last) == store.previous_trade_date(last).isoformat()
    with pytest.raises(CalendarCoverageError, match="next trade date UNKNOWN"):
        ctx.next_trade_date(root, last)
    assert ctx.next_trade_date(root, first) == store.next_trade_date(first).isoformat()
    with pytest.raises(CalendarCoverageError, match="previous trade date UNKNOWN"):
        ctx.previous_trade_date(root, first)


def test_owned_pipelines_read_same_previous_day_as_calendar(root, monkeypatch):
    import json
    from engine_core.data_pipeline import adjust_factor, price_volume, td_sink
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    day = TradeCalendarStore(root).all_trading_dates()[-1]
    expected = TradeCalendarStore(root).previous_trade_date(day).isoformat()
    fixture = Path(__file__).parent / 'fixtures/price_volume_c852/daily_kline.jsonl'
    bar = json.loads(fixture.read_text().splitlines()[0])
    reads = []

    class TD:
        def __init__(self, *args): pass
        def read_day_rows(self, requested):
            reads.append(requested)
            return {bar['symbol']: bar} if requested == day else {}
        def close(self): pass

    class Facts:
        def __init__(self, store): self.store = store
        def ensure_schema(self): pass
        def listing_symbols(self, *args, **kwargs): return set(), 'fallback'

    class Lease:
        def acquire(self): return True
        def release(self): pass

    monkeypatch.setattr(td_sink, 'TaosStore', TD)
    monkeypatch.setattr(adjust_factor, 'AdjustFactorStore', Facts)
    monkeypatch.setattr(price_volume, 'PriceVolumeStore', Facts)
    adjust_factor.run({'trade_date': day, 'args': {'mode': 'incremental'}}, root,
                      lease_factory=lambda *_: Lease())
    assert reads == [day, expected]
    reads.clear()
    price_volume.run({'trade_date': day}, root)
    assert reads == [day, expected]
