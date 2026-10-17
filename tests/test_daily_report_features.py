from engine_core.daily_history import build_daily_report_features
from engine_core.live_session import daily_history_context


def test_daily_report_features_match_existing_history_context():
    rows = []
    for index in range(80):
        day = f"2026-07-{index + 1:02d}"
        rows.append({"symbol": "000001", "date": day, "open": 10 + index / 100,
                     "close": 10 + index / 100, "preclose": 9.99 + index / 100,
                     "amount_yuan": 100 + index})
    features = build_daily_report_features(rows, trade_date="2026-07-80")
    by_symbol = {row["symbol"]: row for row in features if row["symbol"] != "__MARKET__"}
    assert by_symbol["000001"]["position_close_days"] == 20
    assert by_symbol["000001"]["range20_low"] == 10.6
    assert by_symbol["000001"]["range20_high"] == 10.79
    assert len(by_symbol["000001"]["amount_history_64"]) == 64
    market = next(row for row in features if row["symbol"] == "__MARKET__")
    assert market["opening_history_72"][-1]["date"] == "2026-07-80"


def test_daily_report_features_leave_short_windows_missing():
    features = build_daily_report_features([
        {"symbol": "600000", "date": "2026-10-15", "open": 10, "close": 10,
         "preclose": 9.9, "amount_yuan": 100},
    ], trade_date="2026-10-15")
    row = features[0]
    assert row["position_close_days"] == 1
    assert row["range20_low"] is None
    assert row["range20_high"] is None
    assert row["amount_history_64"] is None


def test_materialized_context_matches_existing_context():
    rows = []
    for index in range(30):
        day = f"2026-09-{index + 1:02d}"
        rows.append({"symbol": "600000", "date": day, "open": 10 + index / 10,
                     "close": 10 + index / 10, "preclose": 10 + (index - 1) / 10,
                     "amount_yuan": 100 + index})
    expected = daily_history_context(rows, "2026-09-31T23:59:59", {"600000": {}})
    features = build_daily_report_features(rows, trade_date="2026-09-31")
    actual = daily_history_context(features, "2026-10-01", {"600000": {}})
    assert actual == expected
