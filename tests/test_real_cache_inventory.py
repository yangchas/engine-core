from __future__ import annotations

import json

from examples.run_real_cache_inventory import build_inventory, summarize_key


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.reads = []

    def type(self, key):
        self.reads.append(("type", key))
        value = self.values.get(key)
        if value is None:
            return b"none"
        return value[0].encode()

    def hscan_iter(self, key):
        self.reads.append(("hscan", key))
        yield from self.values[key][1].items()

    def hlen(self, key):
        self.reads.append(("hlen", key))
        return len(self.values[key][1])

    def get(self, key):
        self.reads.append(("get", key))
        return self.values[key][1]

    def strlen(self, key):
        self.reads.append(("strlen", key))
        return len(self.values[key][1])


class ChangingHashRedis(FakeRedis):
    def __init__(self, values):
        super().__init__(values)
        self._hlen_calls = 0

    def hlen(self, key):
        self._hlen_calls += 1
        self.reads.append(("hlen", key))
        return len(self.values[key][1]) - (1 if self._hlen_calls == 1 else 0)


def test_hash_inventory_is_date_aware_and_counts_bad_json():
    client = FakeRedis(
        {
            "cache:hot_plates:2026-09-10": (
                "hash",
                {
                    b"plate-a": json.dumps({"trade_date": "2026-09-10", "source": "kaipan"}).encode(),
                    b"plate-b": json.dumps({"trade_date": "2026-09-09", "source": "kaipan"}).encode(),
                    b"broken": b"not-json",
                },
            )
        }
    )

    result = summarize_key(client, "cache:hot_plates:2026-09-10", "2026-09-10")

    assert result["redis_type"] == "hash"
    assert result["hlen"] == 3
    assert result["decoded_json_count"] == 2
    assert result["invalid_json_count"] == 1
    assert result["exact_trade_date_count"] == 1
    assert result["trade_dates"] == ["2026-09-09", "2026-09-10"]
    assert result["sources"] == ["kaipan"]
    assert result["scan_consistent"] is True
    assert result["scan_item_count"] == result["hlen"] == 3

    reversed_client = FakeRedis(
        {
            "cache:hot_plates:2026-09-10": (
                "hash",
                dict(reversed(list(client.values["cache:hot_plates:2026-09-10"][1].items()))),
            )
        }
    )
    assert summarize_key(
        reversed_client,
        "cache:hot_plates:2026-09-10",
        "2026-09-10",
    )["value_sha256"] == result["value_sha256"]


def test_string_meta_is_read_as_string_not_hash():
    client = FakeRedis(
        {
            "cache:hot_plates_meta:2026-09-10": (
                "string",
                json.dumps(
                    {
                        "trade_date": "2026-09-10",
                        "source": "kaipan",
                        "row_count": 50,
                        "success": True,
                        "updated_at": "2026-09-10 17:40:06",
                        "updated_at_ts": 1789033206,
                    }
                ).encode(),
            )
        }
    )

    result = summarize_key(
        client,
        "cache:hot_plates_meta:2026-09-10",
        "2026-09-10",
    )

    assert result["redis_type"] == "string"
    assert result["meta_json"]["trade_date_matches_request"] is True
    assert result["meta_json"]["row_count"] == 50
    assert "available_at_ms" not in result["meta_json"]["metadata_fields"]
    assert result["meta_json"]["available_at_ms"] is None
    assert result["meta_json"]["field_units"] is None
    assert result["meta_json"]["updated_at_ts"] == 1789033206
    assert ("hscan", "cache:hot_plates_meta:2026-09-10") not in client.reads


def test_hash_inventory_marks_scan_as_inconsistent_when_count_changes():
    client = ChangingHashRedis(
        {
            "cache:hot_plates:2026-09-10": (
                "hash",
                {b"plate-a": b"{}", b"plate-b": b"{}"},
            )
        }
    )

    result = summarize_key(client, "cache:hot_plates:2026-09-10", "2026-09-10")

    assert result["hlen_before_scan"] == 1
    assert result["hlen"] == 2
    assert result["scan_item_count"] == 2
    assert result["scan_consistent"] is False


def test_build_inventory_uses_explicit_date_dialects():
    client = FakeRedis({})

    result = build_inventory(client, "2026-09-10", "2026-09-09")

    keys = result["keys"]
    assert "cache:yest_limit_pool:2026-09-09" in keys
    assert "cache:hot_plates:2026-09-10" in keys
    assert "config:plate_mapping:s2p" in keys
    assert keys["config:plate_mapping:info"]["redis_type"] == "none"
    assert keys["config:plate_mapping:full_sync_info"]["redis_type"] == "none"
    assert result["side_effect_boundary"].startswith("Redis TYPE/EXISTS/HSCAN")


def test_build_inventory_rejects_non_chronological_dates():
    client = FakeRedis({})

    try:
        build_inventory(client, "2026-09-09", "2026-09-10")
    except ValueError as exc:
        assert "earlier" in str(exc)
    else:
        raise AssertionError("expected previous date validation")


def test_build_inventory_rejects_non_strict_dates():
    client = FakeRedis({})

    try:
        build_inventory(client, "2026-9-10", "2026-09-09")
    except ValueError as exc:
        assert "strict" in str(exc)
    else:
        raise AssertionError("expected strict date validation")
