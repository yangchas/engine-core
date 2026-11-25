from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine_core.quote_ingest.contracts import QuoteCursor
from engine_core.quote_ingest.shared_source import SharedQuoteSource
from engine_core.quote_ingest.source_policy import choose_source


class Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.keys = []

    def hgetall(self, key):
        self.keys.append(key)

    def execute(self):
        return [self.redis.hashes.get(key, {}) for key in self.keys]


class Redis:
    def __init__(self):
        self.hashes = {"q2:000001": {"px": "11540", "pc": "11400", "amt": "12", "vol": "0", "ts": "1792134002000", "a20": "0"}}
        self.calls = []

    def smembers(self, key):
        self.calls.append(("smembers", key))
        return {b"000001", b"000002"}

    def pipeline(self, transaction=False):
        self.calls.append(("pipeline", transaction))
        return Pipeline(self)


def test_shared_read_preserves_missing_and_unknown_availability():
    redis = Redis()
    now = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)
    result = SharedQuoteSource(redis=redis, trade_date="2026-10-16", clock=lambda: now).read_since()
    assert result.expected_symbols == 2
    assert result.rows[0].price_milli == 11540
    assert result.rows[0].available_at is None
    assert "AVAILABLE_AT_UNKNOWN" in result.rows[0].quality
    assert result.rows[0].open_milli is None
    assert result.rows[1].price_milli is None
    assert "HASH_MISSING" in result.rows[1].quality
    assert all(call[0] != "scan_iter" for call in redis.calls)


def test_source_selection_requires_explicit_policy_and_does_not_fallback():
    with pytest.raises(ValueError, match="explicit source"):
        choose_source({"legacy_shared_q2": "q2", "legacy_td": "td"})
    assert choose_source({"legacy_shared_q2": "q2", "legacy_td": "td"}, "legacy_td") == "td"


def test_td_adapter_is_injected_and_cursor_is_not_claimed_lossless():
    class TD:
        def read_since(self, cursor, deadline):
            return [{"symbol": "000001", "ts": 123, "px_milli": 10000}]

    source = SharedQuoteSource(redis=Redis(), trade_date="2026-10-16", td_reader=TD())
    result = source.read_since(QuoteCursor(round_id=7), deadline=9999999999, source="legacy_td")
    assert result.cursor.round_id == 8
    assert result.incremental_completeness == "UNKNOWN"
    assert result.rows[0].source == "legacy_td"
