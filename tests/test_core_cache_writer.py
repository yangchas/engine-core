import hashlib
import json

import pytest

from engine_core.core_cache_writer import CoreCacheWriter, verify_core_cache


class FakeClient:
    def __init__(self):
        self.calls = []
        self.hashes = {}
        self.values = {}

    def hset(self, key, mapping):
        self.calls.append(("hset", key, dict(mapping)))
        self.hashes[key] = dict(mapping)

    def set(self, key, value, ex=None):
        self.calls.append(("set", key, value, ex))
        self.values[key] = value

    def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))

    def rename(self, source, target):
        self.calls.append(("rename", source, target))
        self.hashes[target] = self.hashes.pop(source)

    def delete(self, *keys):
        self.calls.append(("delete", *keys))

    def ping(self):
        self.calls.append(("ping",))


def write(writer, rows, ttl_seconds=60):
    return writer.write_hash_dataset(
        "limit_pool",
        "2026-10-12",
        rows,
        source="fixture",
        field_units={"turnover": "yuan"},
        schema_version="LimitPoolV1",
        ttl_seconds=ttl_seconds,
    )


def test_non_core_key_is_rejected_without_client_call():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)

    with pytest.raises(ValueError):
        writer.client.hset("cache:legacy", mapping={"a": "b"})
    with pytest.raises(ValueError):
        writer.client.rename("core:cache:tmp:x", "market:bad")
    assert client.calls == []


def test_client_proxy_rejects_non_whitelisted_method():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)

    with pytest.raises(RuntimeError):
        writer.client.ping()
    assert client.calls == []


def test_write_renames_payload_before_writing_metadata():
    client = FakeClient()
    client.hashes["core:cache:limit_pool:2026-10-12"] = {"old": "row"}
    writer = CoreCacheWriter(client, now_ms=123)

    result = write(writer, {"000001": {"symbol": "000001", "turnover": 10}})

    assert result["written"] is True
    names = [call[0] for call in client.calls]
    assert names.index("hset") < names.index("rename") < names.index("set")
    assert client.hashes["core:cache:limit_pool:2026-10-12"] == {
        "000001": '{"symbol": "000001", "turnover": 10}'
    }
    assert "old" not in client.hashes["core:cache:limit_pool:2026-10-12"]
    assert json.loads(client.values["core:cache:meta:limit_pool:2026-10-12"])["contract"] == "CoreCacheMetaV1"


def test_empty_rows_preserve_previous_payload_and_metadata():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)

    result = write(writer, {})

    assert result == {"written": False, "reason": "EMPTY_ROWS_PRESERVES_PREVIOUS"}
    assert client.calls == []


def test_metadata_hash_verifies_and_detects_payload_tampering():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)
    write(writer, {"000001": {"symbol": "000001", "turnover": 10}})
    meta = json.loads(client.values["core:cache:meta:limit_pool:2026-10-12"])
    rows = client.hashes["core:cache:limit_pool:2026-10-12"]

    assert verify_core_cache(rows, meta) == {"valid": True, "reasons": []}
    assert verify_core_cache({**rows, "000002": "{}"}, meta)["valid"] is False
    assert verify_core_cache(rows, {**meta, "available_at_ms": True})["valid"] is False


def test_ttl_is_applied_to_temporary_and_target_payload_and_metadata():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)
    write(writer, {"000001": "row"}, ttl_seconds=321)

    assert ("expire", "core:cache:tmp:limit_pool:2026-10-12:123", 321) in client.calls
    assert ("expire", "core:cache:limit_pool:2026-10-12", 321) in client.calls
    assert client.calls[-1][0] == "set"
    assert client.calls[-1][1] == "core:cache:meta:limit_pool:2026-10-12"
    assert client.calls[-1][3] == 321


def test_mapping_values_are_stably_serialized_and_hashed_by_symbol():
    client = FakeClient()
    writer = CoreCacheWriter(client, now_ms=123)
    rows = {
        "000002": {"b": 2, "a": "值"},
        "000001": {"z": 1},
    }
    write(writer, rows)
    payload = client.hashes["core:cache:limit_pool:2026-10-12"]
    expected_payload = json.dumps(
        {"000001": rows["000001"], "000002": rows["000002"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    meta = json.loads(client.values["core:cache:meta:limit_pool:2026-10-12"])

    assert payload["000002"] == '{"a": "值", "b": 2}'
    assert meta["payload_sha256"] == hashlib.sha256(expected_payload).hexdigest()
    assert verify_core_cache(payload, meta)["valid"] is True
