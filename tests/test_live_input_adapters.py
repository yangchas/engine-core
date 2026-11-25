import json

from engine_core.live_input_adapters import (
    json_rows_from_state,
    plate_mapping_from_states,
    previous_day_limit_pool_from_state,
)
from engine_core.live_inputs import LiveInputCache, LiveInputReader
from tests.test_live_inputs import StrictClient

POOL_KEY = "cache:yest_limit_pool:2026-10-12"


def _row(symbol, lb=1):
    return json.dumps({
        "symbol": symbol, "name": "n" + symbol, "trade_date": "2026-10-12",
        "close_pct": 10.0, "lb_days": lb, "turnover": 1.0e8, "plate": "x",
        "seal_time": "", "source": "kaipan",
    })


def _state(hashes, t=5000, cache=None, max_age=None):
    cache = cache or LiveInputCache(max_age_ms=max_age)
    reader = LiveInputReader(StrictClient(hashes=hashes), now_ms=lambda: t)
    return cache.refresh(reader.read(
        "yest_limit_pool", trade_date="2026-10-13", previous_trade_date="2026-10-12"))


def _pool(state):
    return previous_day_limit_pool_from_state(
        state, trade_date="2026-10-13", previous_trade_date="2026-10-12",
        cutoff_ms=1_791_854_000_000,
    )


def test_live_pool_state_serves_rows_partial_without_verified_availability():
    result = _pool(_state({POOL_KEY: {b"000001": _row("000001", 2).encode(), b"600000": _row("600000").encode()}}))
    assert result.data["row_count"] == 2
    assert result.status.value == "PARTIAL"
    assert set(result.data["by_symbol"]) == {"000001", "600000"}


def test_missing_pool_is_not_an_empty_complete_pool():
    result = _pool(_state({}))
    assert result.status.value == "MISSING"
    assert result.completeness == 0.0
    assert result.data["row_count"] == 0


def test_expired_preserved_pool_yields_no_rows():
    cache = LiveInputCache(max_age_ms={"yest_limit_pool": 100})
    _state({POOL_KEY: {b"000001": _row("000001").encode()}}, t=0, cache=cache, max_age={"yest_limit_pool": 100})
    expired = _state({}, t=1000, cache=cache)
    assert expired.status == "EXPIRED_PREVIOUS"
    rows, _ = json_rows_from_state(expired)
    assert rows  # evidence stays visible
    assert _pool(expired).status.value == "MISSING"


def test_plate_mapping_states():
    cache = LiveInputCache()
    reader = LiveInputReader(StrictClient(hashes={
        "market:stock_plate": {b"000001": b"AI,Chip"},
        "config:plate_mapping:s2p": {b"000001": b'["AI","Chip"]', b"000002": b"oops"},
    }), now_ms=lambda: 1)
    sp = cache.refresh(reader.read("stock_plate", trade_date="2026-10-13"))
    s2p = cache.refresh(reader.read("s2p", trade_date="2026-10-13"))
    mapping = plate_mapping_from_states(sp, s2p)
    assert mapping["stock_plate"] == {"000001": "AI,Chip"}
    assert mapping["s2p"] == {"000001": ["AI", "Chip"]}
    assert mapping["s2p_invalid_count"] == 1


def test_bad_pool_rows_are_isolated_not_pool_wide_failure():
    rows = {
        b"000001": _row("000001", 2).encode(),
        b"000002": json.dumps({"symbol": "000002", "name": "n", "trade_date": "2026-10-12",
                               "close_pct": 10.0, "lb_days": 1, "plate": "x",
                               "seal_time": "", "source": "kaipan"}).encode(),  # no turnover
        b"000003": b"{broken json",
        b"600000": _row("600000").encode(),
    }
    result = _pool(_state({POOL_KEY: rows}))
    assert result.status.value == "PARTIAL"
    assert result.data["row_count"] == 2
    assert result.data["rejected_count"] == 2
    assert result.completeness == 0.5
    assert result.missing_fields == ("rejected_rows",)
    symbols = {r["symbol"] for r in result.data["rejected_rows"]}
    assert symbols == {"000002", "000003"}


def test_all_rows_bad_is_missing_with_rejections_visible():
    result = _pool(_state({POOL_KEY: {b"000001": b"[1]"}}))
    assert result.status.value == "MISSING"
    assert result.data["rejected_count"] == 1


def test_rejected_pool_rows_mark_feedback_structure_and_report_as_lower_bound():
    from engine_core.facts import FactStatus
    from engine_core.market_auction_report import build_market_auction_report
    from engine_core.previous_day_limit_feedback import build_previous_day_limit_feedback
    from engine_core.previous_day_limit_structure import build_previous_day_limit_structure

    bad = {b"000002": b"{broken"}
    pool = _pool(_state({POOL_KEY: {b"000001": _row("000001", 3).encode(),
                                    b"600000": _row("600000").encode(), **bad}}))
    current = [
        {"symbol": "000001", "trade_date": "2026-10-13", "tag": "0925",
         "change_pct": 1.5, "auction_amount_yuan": 10, "source_record_time_ms": 1},
        {"symbol": "600000", "trade_date": "2026-10-13", "tag": "0925",
         "change_pct": -0.5, "auction_amount_yuan": 10, "source_record_time_ms": 1},
    ]
    feedback = build_previous_day_limit_feedback(pool, current, current_trade_date="2026-10-13")
    structure = build_previous_day_limit_structure(pool)
    for fact in (feedback, structure):
        assert fact.status is FactStatus.PARTIAL
        assert fact.excluded_row_count == 1
        assert fact.as_mapping()["counts_are_lower_bound"] is True
    assert feedback.prior_limit_up_count == 2
    assert "previous.excluded_rows:1" in feedback.missing_fields
    report = build_market_auction_report(
        trade_date="2026-10-13", event_id="E", data_origin="T",
        previous_limit_structure=structure, previous_limit_feedback=feedback,
    )
    assert report.text.count("下限, 另有 1 行被拒绝") == 2
    assert "≥3" in report.text


def test_no_rejected_rows_keeps_exact_counts_and_hash_inputs_unchanged():
    from engine_core.previous_day_limit_structure import build_previous_day_limit_structure

    pool = _pool(_state({POOL_KEY: {b"000001": _row("000001").encode()}}))
    structure = build_previous_day_limit_structure(pool)
    assert structure.excluded_row_count == 0
    assert structure.as_mapping()["counts_are_lower_bound"] is False


def test_probe_summary_matches_legacy_probe_shape_and_keeps_rows_as_stored():
    from engine_core.live_input_adapters import probe_rows_and_summary

    key = "cache:hot_plates:2026-10-13"
    reader = LiveInputReader(StrictClient(hashes={key: {
        b"AI": b'{"plate_name":"AI","rank":1}', b"bad": b"[1]", b"broken": b"{x",
    }}), now_ms=lambda: 1)
    rows, summary = probe_rows_and_summary(reader.read("hot_plates", trade_date="2026-10-13"))
    assert rows == [{"plate_name": "AI", "rank": 1}]  # no symbol injected
    assert summary["decode_errors"] == 2 and summary["row_count"] == 1
    assert summary["hlen_before_scan"] == summary["hlen_after_scan"] == 3
    assert summary["scan_item_count"] == 3 and summary["scan_consistent"] is True
    assert len(summary["payload_value_sha256"]) == 64
    none_rows, none_summary = probe_rows_and_summary(
        LiveInputReader(StrictClient(), now_ms=lambda: 1).read("hot_plates", trade_date="2026-10-13"))
    assert none_rows == [] and none_summary["redis_type"] == "none" and none_summary["meta"] is None
