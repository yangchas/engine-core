import hashlib
import json

import pytest

from examples.seal_plate_snapshot import ReadOnlyRedis, seal_plate_snapshot


class FakeRedis:
    def __init__(self, hashes=None, strings=None):
        self.hashes = hashes or {}
        self.strings = strings or {}

    def type(self, key):
        if key in self.hashes:
            return b"hash"
        return b"string" if key in self.strings else b"none"

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def hscan_iter(self, key):
        return iter(self.hashes.get(key, {}).items())

    def get(self, key):
        return self.strings.get(key)

    def ttl(self, key):
        return 100

    def set(self, *args, **kwargs):
        raise AssertionError("write method called")


def _client():
    return FakeRedis(hashes={
        "market:stock_plate": {b"000001": "银行".encode()},
        "config:plate_mapping:s2p": {b"000001": '["金融", "银行"]'.encode(), b"000002": b"bad"},
        "market:stock_reason": {},
        "cache:yest_limit_pool:2026-10-12": {b"000001": b'{"lb_days":2}'},
        "cache:hot_plates:2026-10-13": {b"p1": '{"name":"银行"}'.encode()},
    }, strings={
        "cache:yest_limit_pool_meta:2026-10-12": b'{"phase":"auction"}',
        "cache:hot_plates_meta:2026-10-13": b'{"phase":"auction"}',
    })


def test_seal_rejects_methods_outside_read_whitelist(tmp_path):
    with pytest.raises(AttributeError):
        ReadOnlyRedis(_client()).delete("market:stock_plate")


def test_manifest_hashes_match_written_files(tmp_path):
    output = seal_plate_snapshot(_client(), trade_date="2026-10-13",
                                 previous_trade_date="2026-10-12", out_root=tmp_path,
                                 now_ms=lambda: 1234)
    manifest = json.loads((output / "manifest.json").read_text())
    for name, details in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == details["sha256"]


def test_seal_refuses_existing_trade_date_directory(tmp_path):
    (tmp_path / "2026-10-13").mkdir()
    with pytest.raises(FileExistsError):
        seal_plate_snapshot(_client(), trade_date="2026-10-13",
                            previous_trade_date="2026-10-12", out_root=tmp_path,
                            now_ms=lambda: 1234)


def test_empty_hash_has_zero_rows_without_synthetic_data(tmp_path):
    output = seal_plate_snapshot(_client(), trade_date="2026-10-13",
                                 previous_trade_date="2026-10-12", out_root=tmp_path,
                                 now_ms=lambda: 1234)
    assert json.loads((output / "stock_reason.json").read_text()) == []
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["files"]["stock_reason.json"]["row_count"] == 0


def test_s2p_decodes_json_lists_and_counts_invalid_entries(tmp_path):
    output = seal_plate_snapshot(_client(), trade_date="2026-10-13",
                                 previous_trade_date="2026-10-12", out_root=tmp_path,
                                 now_ms=lambda: 1234)
    assert json.loads((output / "s2p.json").read_text()) == {
        "000001": ["金融", "银行"], "000002": "bad"
    }
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["files"]["s2p.json"]["invalid_entries"] == 1
