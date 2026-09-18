import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from engine_core import semantic_hash


SPEC = importlib.util.spec_from_file_location(
    "real_redis_td_projection_compare",
    Path(__file__).parents[1] / "examples" / "run_real_redis_td_projection_compare.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeRedis:
    def __init__(self, hashes, strings):
        self.hashes = hashes
        self.strings = strings

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def get(self, key):
        return self.strings.get(key)


def _redis_client():
    hashes = {}
    for tag, ts, amount, bid, ask in (
        ("0920", 1788916800000, 100, 10, 20),
        ("0924", 1788917040000, 200, 30, 40),
        ("0925", 1788917100000, 300, 50, 60),
    ):
        hashes[f"market:auction:20260909:{tag}"] = {
            "meta": json.dumps({"tag": tag, "ts": ts, "n": 1}),
            "summary": json.dumps({"tag": tag, "ts": ts}),
            "top_amount": json.dumps([
                {
                    "symbol": "600519",
                    "price": 1305,
                    "auction_amount_yuan": amount,
                    "bid_amount_yuan": bid,
                    "ask_amount_yuan": ask,
                }
            ]),
        }
    return FakeRedis(
        hashes,
        {
            "market:auction:anchor:20260909": json.dumps({
                "600519": {
                    "amount": 300,
                    "bid_amount": 50,
                    # Real legacy anchor samples may omit ask_amount.
                    "tag": "0925",
                    "source": "redis_0925",
                }
            }),
        },
    )


def _td_rows():
    return [
        (datetime(2026, 9, 9, 9, 20), 1305000, 100, 10, 20, "600519", "20260909", "0920"),
        (datetime(2026, 9, 9, 9, 24), 1305000, 200, 30, 40, "600519", "20260909", "0924"),
        (datetime(2026, 9, 9, 9, 25), 1305000, 300, 50, 60, "600519", "20260909", "0925"),
    ]


def test_real_projection_compares_shared_fields_and_marks_missing_fields():
    redis_data = MODULE._read_redis(_redis_client(), "2026-09-09", ("600519",))
    result = MODULE.compare_projections(redis_data, _td_rows())

    assert result["summary"] == {
        "match": 2,
        "partial_comparable": 1,
        "not_comparable": 0,
        "mismatch": 0,
        "timestamp_mismatch": 0,
    }
    by_tag = {item["tag"]: item for item in result["comparisons"]}
    assert by_tag["0920"]["status"] == "MATCH"
    assert by_tag["0925"]["status"] == "PARTIAL_COMPARABLE"
    assert by_tag["0925"]["fields"]["rest_ask_amt_yuan"]["status"] == "NOT_COMPARABLE"
    assert by_tag["0925"]["fields"]["rest_ask_amt_yuan"]["reason"] == "redis_anchor_field_absent"
    assert len(result["semantic_hash"]) == 64
    payload = {key: value for key, value in result.items() if key != "semantic_hash"}
    assert result["semantic_hash"] == semantic_hash(payload)


def test_real_redis_reader_uses_core_projection_adapter(monkeypatch):
    calls = []
    original = MODULE.read_redis_auction_projection

    def spy(client, **kwargs):
        calls.append(kwargs)
        return original(client, **kwargs)

    monkeypatch.setattr(MODULE, "read_redis_auction_projection", spy)
    MODULE._read_redis(_redis_client(), "2026-09-09", ("600519",))

    assert len(calls) == 1
    assert calls[0]["trade_date"] == "2026-09-09"
    assert calls[0]["tags"] == MODULE.TAGS
    assert calls[0]["symbols"] == ("600519",)
    assert isinstance(calls[0]["observed_at_ms"], int)


def test_real_projection_does_not_call_absent_top_rows_equal():
    redis_data = MODULE._read_redis(_redis_client(), "2026-09-09", ("000001",))
    rows = [(*row[:5], "000001", *row[6:]) for row in _td_rows()]
    result = MODULE.compare_projections(redis_data, rows)

    assert result["summary"]["not_comparable"] == 3
    assert result["summary"]["mismatch"] == 0
    assert all(item["status"] == "NOT_COMPARABLE" for item in result["comparisons"])
    assert all(
        item["comparability_reason"] == "symbol_outside_redis_top_amount_window"
        for item in result["comparisons"]
    )


def test_real_projection_uses_declared_top_amount_count_after_selected_capture():
    redis_data = {
        "trade_date": "2026-09-09",
        "symbols": ("000001",),
        "snapshots": {
            tag: {
                "meta": {"ts": ts},
                "top_amount": [{"symbol": "600519"}],
                "top_amount_count": 1,
            }
            for tag, ts in (("0920", 1788916800000), ("0924", 1788917040000), ("0925", 1788917100000))
        },
        "anchor": {"rows": {}},
    }
    rows = [(*row[:5], "000001", *row[6:]) for row in _td_rows()]
    result = MODULE.compare_projections(redis_data, rows)
    assert all(
        item["comparability_reason"] == "symbol_outside_redis_top_amount_window"
        for item in result["comparisons"]
    )


def test_real_projection_reports_shared_field_mismatch():
    redis_data = MODULE._read_redis(_redis_client(), "2026-09-09", ("600519",))
    redis_data["anchor"]["rows"]["600519"]["amount"] = 301
    result = MODULE.compare_projections(redis_data, _td_rows())

    assert result["summary"]["mismatch"] == 1
    assert {item["tag"] for item in result["comparisons"] if item["status"] == "MISMATCH"} == {"0925"}


def test_real_projection_parses_driver_timestamp_strings_after_json_capture():
    assert MODULE._epoch_ms("2026-09-09 09:20:00+08:00") == 1788916800000
    assert MODULE._epoch_ms("2026-09-09T01:20:00+00:00") == 1788916800000


@pytest.mark.parametrize("value", ["20260909", "2026-9-09", "2026-09-9"])
def test_real_projection_requires_strict_trade_date(value):
    with pytest.raises(ValueError):
        MODULE._strict_date(value)
