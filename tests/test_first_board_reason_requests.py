from engine_core import plan_first_board_reason_requests


def test_new_limit_up_without_asof_reason_is_requested():
    plan = plan_first_board_reason_requests(
        trade_date="2026-10-13",
        limit_state_by_symbol={"600001": 1},
        yest_pool_symbols=(),
        reasons_by_symbol={"600001": [{"Date": "2026-10-12"}]},
        already_requested=(),
    )

    assert plan.to_request == ("600001",)
    assert plan.skipped == {}
    assert plan.content_hash


def test_pool_asof_reason_and_already_requested_symbols_are_skipped():
    plan = plan_first_board_reason_requests(
        trade_date="2026-10-13",
        limit_state_by_symbol={"000001": 1, "000002": 1, "000003": 1},
        yest_pool_symbols={"000001"},
        reasons_by_symbol={"000002": [{"Date": "2026-10-13"}]},
        already_requested={"000003"},
    )

    assert plan.to_request == ()
    assert plan.skipped == {
        "000001": "IN_POOL",
        "000002": "HAS_ASOF_REASON",
        "000003": "ALREADY_REQUESTED",
    }


def test_unknown_and_non_limitup_states_are_skipped():
    plan = plan_first_board_reason_requests(
        trade_date="2026-10-13",
        limit_state_by_symbol={"000001": None, "000002": -1, "000003": 0},
        yest_pool_symbols=(),
        reasons_by_symbol={},
        already_requested=(),
    )

    assert plan.to_request == ()
    assert plan.skipped == {
        "000001": "STATE_UNKNOWN",
        "000002": "NOT_LIMIT_UP",
        "000003": "NOT_LIMIT_UP",
    }


def test_request_order_is_stable_and_budget_is_reported():
    plan = plan_first_board_reason_requests(
        trade_date="2026-10-13",
        limit_state_by_symbol={"600003": 1, "600001": 1, "600002": 1},
        yest_pool_symbols=(),
        reasons_by_symbol={},
        already_requested=(),
        max_requests=2,
    )

    assert plan.to_request == ("600001", "600002")
    assert plan.skipped == {"600003": "OVER_BUDGET"}
