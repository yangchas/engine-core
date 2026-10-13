import json

import pytest

from engine_core.live_inputs import (
    EXPIRED_PREVIOUS,
    FRESH,
    MISSING,
    PRESERVED_PREVIOUS,
    LiveInputCache,
    LiveInputReader,
)


class StrictClient:
    """Allows only the read operations the layer is documented to use."""

    def __init__(self, hashes=None, strings=None, flap=False):
        self.hashes = hashes or {}
        self.strings = strings or {}
        self.flap = flap
        self.calls = []
        self._hlen_calls = 0

    def type(self, key):
        self.calls.append("type")
        if key in self.hashes:
            return b"hash"
        return b"string" if key in self.strings else b"none"

    def hlen(self, key):
        self.calls.append("hlen")
        self._hlen_calls += 1
        n = len(self.hashes.get(key, {}))
        return n + (1 if self.flap and self._hlen_calls % 2 == 0 else 0)

    def hscan_iter(self, key):
        self.calls.append("hscan_iter")
        return iter(self.hashes.get(key, {}).items())

    def get(self, key):
        self.calls.append("get")
        return self.strings.get(key)

    def ttl(self, key):
        self.calls.append("ttl")
        return 100

    def __getattr__(self, name):
        raise AssertionError("write/other op not allowed: " + name)


def _reader(client, t=1000):
    return LiveInputReader(client, now_ms=lambda: t)


def test_hash_read_keys_and_only_read_ops():
    client = StrictClient(hashes={"cache:hot_rank:2026-10-13": {b"600000": b'{"rank":1}'}})
    read = _reader(client).read("hot_rank", trade_date="2026-10-13")
    assert read.key == "cache:hot_rank:2026-10-13"
    assert read.rows == (("600000", '{"rank":1}'),)
    assert read.usable
    assert set(client.calls) <= {"type", "hlen", "hscan_iter"}


def test_pool_uses_previous_trade_date_and_requires_it():
    reader = _reader(StrictClient())
    assert reader.key_for(
        "yest_limit_pool", trade_date="2026-10-13", previous_trade_date="2026-10-12"
    ) == "cache:yest_limit_pool:2026-10-12"
    with pytest.raises(ValueError):
        reader.key_for("yest_limit_pool", trade_date="2026-10-13")
    with pytest.raises(ValueError):
        reader.key_for("nope", trade_date="2026-10-13")


def test_anchor_archive_json_and_invalid_json():
    key = "market:auction:anchor:20261013"
    ok = _reader(StrictClient(strings={key: json.dumps({"000001": {"change_pct": 0.01}})})).read(
        "anchor_archive", trade_date="2026-10-13"
    )
    assert ok.usable and ok.ttl_seconds == 100 and "000001" in ok.payload
    bad = _reader(StrictClient(strings={key: "{not json"})).read("anchor_archive", trade_date="2026-10-13")
    assert not bad.usable and not bad.scan_consistent


def test_empty_response_preserves_last_good_and_never_zero_fills():
    key = "cache:hot_plates:2026-10-13"
    cache = LiveInputCache()
    first = cache.refresh(_reader(StrictClient(hashes={key: {b"a": b"{}"}}), 1000).read(
        "hot_plates", trade_date="2026-10-13"))
    assert first.status == FRESH
    again = cache.refresh(_reader(StrictClient(hashes={}), 2000).read(
        "hot_plates", trade_date="2026-10-13"))
    assert again.status == PRESERVED_PREVIOUS
    assert again.reason == "empty_response"
    assert again.read.rows == (("a", "{}"),)
    assert again.last_refreshed_ms == 1000 and again.last_attempt_ms == 2000


def test_missing_without_prior_is_missing_not_empty_data():
    cache = LiveInputCache()
    state = cache.refresh(_reader(StrictClient()).read("hot_rank", trade_date="2026-10-13"))
    assert state.status == MISSING and state.read is None


def test_inconsistent_scan_does_not_replace_good_data():
    key = "cache:hot_rank:2026-10-13"
    cache = LiveInputCache()
    cache.refresh(_reader(StrictClient(hashes={key: {b"a": b"1"}}), 1).read("hot_rank", trade_date="2026-10-13"))
    flapping = StrictClient(hashes={key: {b"a": b"1", b"b": b"2"}}, flap=True)
    state = cache.refresh(_reader(flapping, 2).read("hot_rank", trade_date="2026-10-13"))
    assert state.status == PRESERVED_PREVIOUS and state.reason == "scan_inconsistent"
    assert len(state.read.rows) == 1


def test_preserved_read_expires_after_max_age():
    key = "cache:hot_rank:2026-10-13"
    cache = LiveInputCache(max_age_ms={"hot_rank": 1000})
    cache.refresh(_reader(StrictClient(hashes={key: {b"a": b"1"}}), 0).read("hot_rank", trade_date="2026-10-13"))
    soon = cache.refresh(_reader(StrictClient(), 900).read("hot_rank", trade_date="2026-10-13"))
    late = cache.refresh(_reader(StrictClient(), 1500).read("hot_rank", trade_date="2026-10-13"))
    assert soon.status == PRESERVED_PREVIOUS
    assert late.status == EXPIRED_PREVIOUS and late.read is not None
