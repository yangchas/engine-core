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


def _hot_rank_client(day, rows=None):
    version = "v-test"
    rows = rows or [{"symbol": "a", "rank": 1}]
    payload = json.dumps({"dataset": "hot_rank", "trade_date": day, "version": version,
                          "rows": rows})
    return StrictClient(strings={
        f"core:cache:data:hot_rank:{day}": json.dumps({"trade_date": day, "version": version}),
        f"core:cache:data:hot_rank:{day}:{version}": payload,
    })


def test_hash_read_keys_and_only_read_ops():
    from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "real" / "ths_hot_rank_2026-10-23_1441.json"
    rows = ThsHotRankProvider.normalize(json.loads(fixture.read_text(encoding="utf-8")))
    version = "v-real"
    payload = json.dumps({"dataset": "hot_rank", "trade_date": "2026-10-13",
                          "version": version, "available_at": "2026-10-13T14:41:00+08:00",
                          "rows": rows}, ensure_ascii=False)
    client = StrictClient(strings={
        "core:cache:data:hot_rank:2026-10-13": json.dumps({"trade_date": "2026-10-13", "version": version}),
        "core:cache:data:hot_rank:2026-10-13:v-real": payload,
    })
    read = _reader(client).read("hot_rank", trade_date="2026-10-13")
    assert read.key == "core:cache:data:hot_rank:2026-10-13"
    assert len(read.rows) == len(rows)
    assert json.loads(read.rows[0][1]) == rows[0]
    assert read.usable
    assert client.calls == ["get", "get"]


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


def test_anchor_archive_json_scalar_is_not_usable():
    key = "market:auction:anchor:20261013"
    read = _reader(StrictClient(strings={key: "42"})).read(
        "anchor_archive", trade_date="2026-10-13"
    )
    assert not read.usable and not read.scan_consistent


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


def test_last_good_is_scoped_to_input_name_and_redis_key():
    cache = LiveInputCache()
    first = cache.refresh(_reader(_hot_rank_client("2026-10-12"), 1).read(
        "hot_rank", trade_date="2026-10-12"))
    second = cache.refresh(_reader(StrictClient(), 2).read(
        "hot_rank", trade_date="2026-10-13"))
    assert first.status == FRESH
    assert second.status == MISSING and second.read is None


def test_missing_without_prior_is_missing_not_empty_data():
    cache = LiveInputCache()
    state = cache.refresh(_reader(StrictClient()).read("hot_rank", trade_date="2026-10-13"))
    assert state.status == MISSING and state.read is None


def test_inconsistent_scan_does_not_replace_good_data():
    cache = LiveInputCache()
    cache.refresh(_reader(_hot_rank_client("2026-10-13"), 1).read("hot_rank", trade_date="2026-10-13"))
    flapping = StrictClient()
    state = cache.refresh(_reader(flapping, 2).read("hot_rank", trade_date="2026-10-13"))
    assert state.status == PRESERVED_PREVIOUS and state.reason == "empty_response"
    assert len(state.read.rows) == 1


def test_preserved_read_expires_after_max_age():
    cache = LiveInputCache(max_age_ms={"hot_rank": 1000})
    cache.refresh(_reader(_hot_rank_client("2026-10-13"), 0).read("hot_rank", trade_date="2026-10-13"))
    soon = cache.refresh(_reader(StrictClient(), 900).read("hot_rank", trade_date="2026-10-13"))
    late = cache.refresh(_reader(StrictClient(), 1500).read("hot_rank", trade_date="2026-10-13"))
    assert soon.status == PRESERVED_PREVIOUS
    assert late.status == EXPIRED_PREVIOUS and late.read is not None


def test_meta_key_is_read_for_hash_inputs_only_when_hash_and_decoded():
    key = "cache:hot_plates:2026-10-13"
    meta_key = "cache:hot_plates_meta:2026-10-13"
    ok = StrictClient(hashes={key: {b"a": b"{}"}}, strings={meta_key: '{"row_count": 1}'})
    read = _reader(ok).read("hot_plates", trade_date="2026-10-13")
    assert read.meta == {"row_count": 1} and read.meta_key == meta_key
    bad = StrictClient(hashes={key: {b"a": b"{}"}}, strings={meta_key: "{nope"})
    assert _reader(bad).read("hot_plates", trade_date="2026-10-13").meta == {"valid_json": False}
    absent = StrictClient(strings={meta_key: '{"row_count": 1}'})  # key itself not a hash
    assert _reader(absent).read("hot_plates", trade_date="2026-10-13").meta is None


def test_every_named_input_has_a_default_max_age_and_expires():
    from engine_core.live_inputs import DEFAULT_MAX_AGE_MS

    names = {"hot_plates", "hot_rank", "limit_truth", "yest_limit_pool",
             "stock_plate", "s2p", "anchor_archive"}
    assert names <= set(DEFAULT_MAX_AGE_MS)
    cache = LiveInputCache()
    key = "market:stock_plate"
    cache.refresh(_reader(StrictClient(hashes={key: {b"000001": b"AI"}}), 0).read(
        "stock_plate", trade_date="2026-10-13"))
    ok = cache.refresh(_reader(StrictClient(), DEFAULT_MAX_AGE_MS["stock_plate"]).read(
        "stock_plate", trade_date="2026-10-13"))
    gone = cache.refresh(_reader(StrictClient(), DEFAULT_MAX_AGE_MS["stock_plate"] + 1).read(
        "stock_plate", trade_date="2026-10-13"))
    assert ok.status == PRESERVED_PREVIOUS and gone.status == EXPIRED_PREVIOUS
