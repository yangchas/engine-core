from engine_core.streak_feedback import build_streak_feedback


def test_checkpoint_state_uses_event_priority_and_marks_auction_provisional():
    pool = [
        {"symbol": "600001", "lb_days": 2},
        {"symbol": "600002", "lb_days": 3},
        {"symbol": "600003", "lb_days": 2},
    ]
    snapshots = {"0925": {
        "600001": {"price": 11, "pre_close": 10, "limit_state": 1, "rest_bid": 100},
        "600002": {"price": 9, "pre_close": 10, "limit_state": -1, "rest_ask": 100},
        "600003": {"price": 10.5, "pre_close": 10, "rest_bid": None},
    }}
    result = build_streak_feedback(
        trade_date="2026-10-16", used_date="2026-10-15", version="v1",
        pool=pool, snapshots=snapshots, stage="auction")
    rows = result["checkpoints"]["0925"]["rows"]
    assert [row["status"] for row in rows] == ["PROMOTED", "LIMIT_DOWN", "UNKNOWN"]
    assert result["checkpoints"]["0925"]["provisional"] is True
    assert result["checkpoints"]["0925"]["matrix"]["by_level"]["2板"]["total"] == 2
    assert result["checkpoints"]["0925"]["matrix"]["by_level"]["2板"]["unknown"] == 1
    assert result["checkpoints"]["0925"]["matrix"]["by_level"]["2板"]["promotion_rate"] == 0.5


def test_open_checkpoint_requires_seal_path_for_broken_and_high_for_touch():
    pool = [{"symbol": "600001", "lb_days": 2}, {"symbol": "600002", "lb_days": 2}]
    snapshots = {
        "0932": {
            "600001": {"price": 11, "pre_close": 10, "rest_bid": 100, "high": 11},
            "600002": {"price": 10.5, "pre_close": 10, "rest_bid": 0, "high": 11},
        },
        "0945": {
            "600001": {"price": 10.8, "pre_close": 10, "rest_bid": 0, "high": 11, "return_pct": 8},
            "600002": {"price": 10.2, "pre_close": 10, "rest_bid": 0, "high": 10.5, "return_pct": 2},
        },
    }
    result = build_streak_feedback(
        trade_date="2026-10-16", used_date="2026-10-15", version="v1",
        pool=pool, snapshots=snapshots, stage="open")
    assert result["checkpoints"]["0925"]["provisional"] is True
    assert [row["status"] for row in result["checkpoints"]["0945"]["rows"]] == [
        "BROKEN", "FELL_BACK"]
    assert result["checkpoints"]["0945"]["matrix"]["by_level"]["2板"]["broken"] == 1
    assert result["checkpoints"]["0945"]["matrix"]["overall"]["return_count"] == 2


def test_002343_observed_093009_seal_marks_all_later_open_checkpoints_broken():
    snapshots = {
        "0925": {"002343": {"price": 9.65, "pre_close": 8.91, "rest_bid": 0,
                              "high": 9.65}},
        "0932": {"002343": {"price": 8.93, "pre_close": 8.91, "rest_bid": 5_614_248,
                              "high": 9.80, "prior_seal_observed": True}},
        "0938": {"002343": {"price": 9.16, "pre_close": 8.91, "rest_bid": 672_344,
                              "high": 9.80, "prior_seal_observed": True}},
        "0945": {"002343": {"price": 9.33, "pre_close": 8.91, "rest_bid": 98_898,
                              "high": 9.80, "prior_seal_observed": True}},
    }
    result = build_streak_feedback(
        trade_date="2026-10-16", used_date="2026-10-15", version="v1",
        pool=[{"symbol": "002343", "lb_days": 5}], snapshots=snapshots, stage="open")
    assert [result["checkpoints"][checkpoint]["rows"][0]["status"]
            for checkpoint in ("0932", "0938", "0945")] == ["BROKEN", "BROKEN", "BROKEN"]


def test_theme_matrix_uses_shared_primary_assignment_and_keeps_unknown_explicit():
    result = build_streak_feedback(
        trade_date="2026-10-16", used_date="2026-10-15", version="v1",
        pool=[{"symbol": "600001", "lb_days": 2}, {"symbol": "600002", "lb_days": 2}],
        snapshots={"0945": {
            "600001": {"price": 11, "pre_close": 10, "rest_bid": 10},
            "600002": {"price": None, "pre_close": 10, "rest_bid": None},
        }}, stage="open",
        theme_assignments={"600001": {"primary_theme": {"id": "T1", "name": "主题一"}},
                           "600002": {"primary_theme": {"id": "T2", "name": "主题二"}}})
    assert result["checkpoints"]["0945"]["matrix"]["by_theme"]["T1"]["promoted"] == 1
    assert result["checkpoints"]["0945"]["matrix"]["by_theme"]["T2"]["unknown"] == 1
    assert result["checkpoints"]["0945"]["matrix"]["by_theme"]["T2"]["promotion_rate"] == 0
