"""C636: Baostock bars arrive after midnight; the previous day's feature build must still see its own bar."""
import json

import pytest

from engine_core.daily_history import pit_asof_with_late_inputs
from engine_core.data_pipeline.derived_daily import UpstreamNotReady, _prepare

DAY = "2026-10-30"


class StubCatalog:
    def __init__(self, root, availability):
        self.root, self.data = root, {}
        for day, available in availability.items():
            path = root / f"{day}.json"
            path.write_text(json.dumps({"business_status": "FORMAL", "available_at": available}))
            self.data[day] = [{"manifest": str(path), "business_status": "FORMAL"}]

    def read(self):
        return {"datasets": {"daily_kline": {"dates": self.data}}}


def test_a_bar_published_after_midnight_moves_the_cutoff_to_its_arrival(tmp_path):
    # real shape: 10-28 FORMAL arrived 10-31 01:51, 10-29 on 10-30 01:00, 10-30 on 10-31 01:45
    catalog = StubCatalog(tmp_path, {"2026-10-28": "2026-10-31T01:51:45.705478+08:00",
                                     "2026-10-29": "2026-10-30T01:00:13+08:00",
                                     DAY: "2026-10-31T01:45:35+08:00"})
    assert pit_asof_with_late_inputs(catalog, DAY) == "2026-10-31T01:51:45.705478+08:00"   # the LAST input, not just own


def test_bars_published_the_same_evening_keep_the_end_of_day_cutoff(tmp_path):
    catalog = StubCatalog(tmp_path, {"2026-10-29": "2026-10-29T17:50:00+08:00", DAY: "2026-10-30T18:05:00+08:00"})
    assert pit_asof_with_late_inputs(catalog, DAY) == "2026-10-30T23:59:59+08:00"


def test_later_days_are_not_inputs(tmp_path):
    catalog = StubCatalog(tmp_path, {DAY: "2026-10-30T18:05:00+08:00", "2026-11-02": "2026-11-03T01:00:00+08:00"})
    assert pit_asof_with_late_inputs(catalog, DAY) == "2026-10-30T23:59:59+08:00"


def test_the_end_of_day_cutoff_hides_the_late_bar_and_the_new_cutoff_shows_it(tmp_path):
    availability = {DAY: "2026-10-31T01:45:35+08:00"}
    catalog = StubCatalog(tmp_path, availability)
    part = {"trade_date": DAY, "available_at": availability[DAY], "business_status": "FORMAL", "rows": []}

    def request(as_of):
        return {"trade_date": DAY, "as_of": as_of, "daily_kline": [part], "window": {"max_days": 120}}

    with pytest.raises(UpstreamNotReady) as hidden:                       # what startup_0830 did on 2026-11-02
        _prepare(request(f"{DAY}T23:59:59+08:00"))
    assert "DAILY_KLINE_MISSING" in str(hidden.value.args)
    try:
        _prepare(request(pit_asof_with_late_inputs(catalog, DAY)))
    except UpstreamNotReady as exc:                                       # anything else, but not "own bar missing"
        assert "DAILY_KLINE_MISSING" not in str(exc.args)
    except Exception:
        pass


def test_prep_trade_day_builds_yesterdays_features_with_the_late_input_cutoff():
    import inspect
    from engine_core import prep_trade_day

    source = inspect.getsource(prep_trade_day)
    assert "pit_asof_with_late_inputs(catalog, previous)" in source
    assert 'asof=f"{previous}T23:59:59+08:00"' not in source
