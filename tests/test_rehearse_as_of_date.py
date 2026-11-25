from datetime import date

import pytest

from examples.rehearse_event import resolve_as_of_date


def test_resolve_as_of_date():
    today = date(2026, 11, 7)
    dates = ["2026-11-05", "2026-11-06", "2026-11-09"]
    assert resolve_as_of_date(None, today, dates) == "2026-11-07"
    assert resolve_as_of_date("2026-11-06", today, dates) == "2026-11-06"
    for invalid in ("2026-11-04", "2026-11-07", "2026-11-09", "2026-1-06", "invalid"):
        with pytest.raises(ValueError):
            resolve_as_of_date(invalid, today, dates)
