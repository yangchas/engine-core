import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.node_store import NodeStore, node_fingerprint
from engine_core.data_pipeline.storage import ArtifactCatalog


FIXTURE = Path(__file__).parent / "fixtures" / "real_limit_up_pool_sample.jsonl"


def fixture_rows():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def test_compute_hit_survives_new_store_and_does_not_read_manifest(tmp_path, monkeypatch):
    catalog = ArtifactCatalog(tmp_path)
    rows = fixture_rows()
    store = NodeStore(catalog)
    calls = []
    first, hit = store.compute("sample", "2026-10-21", "v1", {"pool": "sha256-a"},
                               lambda: calls.append(1) or rows)
    assert first == rows and hit is False
    second_store = NodeStore(catalog)
    original = catalog.read_rows
    reads = []

    def tracked(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(catalog, "read_rows", tracked)
    second, hit = second_store.compute("sample", "2026-10-21", "v1", {"pool": "sha256-a"},
                                       lambda: pytest.fail("compute called on hit"))
    assert second == rows and hit is True and calls == [1]
    assert len(reads) == 1


def test_fingerprint_tracks_code_and_inputs_but_not_mapping_order(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    store = NodeStore(catalog)
    rows = fixture_rows()
    base = {"a": "v1", "b": "v2"}
    assert node_fingerprint("n", "v1", base) == node_fingerprint("n", "v1", dict(reversed(list(base.items()))))
    assert store.compute("n", "2026-10-21", "v1", base, lambda: rows)[1] is False
    assert store.compute("n", "2026-10-21", "v2", base, lambda: rows)[1] is False
    assert store.compute("n", "2026-10-21", "v1", {"a": "changed", "b": "v2"}, lambda: rows)[1] is False
    assert store.compute("n", "2026-10-21", "v1", dict(reversed(list(base.items()))),
                         lambda: pytest.fail("mapping order changed fingerprint"))[1] is True


def test_exception_non_list_roundtrip_and_idempotent_put(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    store = NodeStore(catalog)
    with pytest.raises(RuntimeError):
        store.compute("n", "2026-10-21", "v1", {}, lambda: (_ for _ in ()).throw(RuntimeError()))
    assert catalog.read().get("datasets", {}) == {}
    value, hit = store.compute("scalar", "2026-10-21", "v1", {}, lambda: {"ok": 1})
    assert value == {"ok": 1} and hit is False
    assert store.compute("scalar", "2026-10-21", "v1", {}, lambda: None) == ({"ok": 1}, True)
    fp = node_fingerprint("idem", "v1", {})
    store.put("idem", "2026-10-21", fp, fixture_rows(), inputs={}, code_version="v1")
    before = len(catalog.read()["datasets"]["node__idem"]["dates"]["2026-10-21"])
    store.put("idem", "2026-10-21", fp, fixture_rows(), inputs={}, code_version="v1")
    after = len(catalog.read()["datasets"]["node__idem"]["dates"]["2026-10-21"])
    assert before == after == 1
