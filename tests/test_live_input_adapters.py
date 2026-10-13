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
