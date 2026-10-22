import json
from datetime import datetime, timezone
from pathlib import Path

from engine_core.data_pipeline.storage import ArtifactCatalog, available_at_trust
from engine_core.slices import SliceProviders, SliceReader


def fixture_rows():
    return [json.loads(line) for line in Path(__file__).parent.joinpath(
        "fixtures/real_limit_up_pool_sample.jsonl").read_text().splitlines()]


def test_commit_measures_available_at_and_preserves_declared_time(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "core")
    now = datetime(2026, 10, 23, 1, 2, 3, tzinfo=timezone.utc)
    result = catalog.commit("limit_up_pool", "2026-10-21", fixture_rows(),
        {"available_at": "2026-10-21T00:00:00+08:00", "captured_at": "2026-10-23T10:00:00+08:00",
         "as_of": "2026-10-21"}, source="test", clock=lambda: now)
    assert result["available_at"] == result["committed_at"] == "2026-10-23T01:02:03Z"
    assert result["declared_available_at"] == "2026-10-21T00:00:00+08:00"
    assert result["captured_at"] == result["committed_at"]
    assert result["business_date"] == result["trade_date"] == "2026-10-21"
    assert result["source_asof"] == "2026-10-21"
    assert available_at_trust(result) == "MEASURED"


def test_commit_is_idempotent_and_catalog_has_measured_fields(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "core")
    rows = fixture_rows()
    first = catalog.commit("limit_up_pool", "2026-10-21", rows,
        {"available_at": "declared", "captured_at": "2026-10-23T09:00:00+08:00"},
        source="test", authority="IMPORTED",
        clock=lambda: datetime(2026, 10, 23, 1, 2, 3, tzinfo=timezone.utc))
    second = catalog.commit("limit_up_pool", "2026-10-21", rows, {}, source="test",
        clock=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert second["committed_at"] == first["committed_at"]
    assert second["available_at"] == first["available_at"]
    assert second["authority"] == "IMPORTED"
    entry = catalog.read()["datasets"]["limit_up_pool"]["dates"]["2026-10-21"][0]
    assert {key: entry[key] for key in ("available_at", "committed_at", "authority")} == {
        "available_at": first["available_at"], "committed_at": first["committed_at"],
        "authority": "IMPORTED"}


def test_old_catalog_manifest_remains_selectable_as_legacy(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "core")
    manifest = catalog.commit("hot_rank", "2026-10-19", fixture_rows(),
        {"available_at": "2026-10-19T08:00:00+08:00"}, source="test",
        clock=lambda: datetime(2026, 10, 19, 0, tzinfo=timezone.utc))
    path = Path(manifest["manifest_uri"])
    value = json.loads(path.read_text())
    value.pop("committed_at")
    value.pop("authority")
    path.write_text(json.dumps(value))
    entry = catalog.read()["datasets"]["hot_rank"]["dates"]["2026-10-19"][0]
    for key in ("available_at", "committed_at", "authority"):
        entry.pop(key, None)
    from engine_core.slices import SliceReader
    reader = SliceReader()
    providers = SliceProviders(catalog=catalog, redis=None, file_fallback=False)
    assert providers._select_manifest("hot_rank", "2026-10-19",
        "2026-10-19T09:00:00+08:00", reader=reader) == path
    assert available_at_trust(reader.read_manifest(path)) == "DECLARED_LEGACY"


def test_late_version_is_excluded_and_date_fallback_is_partial(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "core")
    rows = fixture_rows()
    def at(value):
        return lambda: datetime.fromisoformat(value.replace("Z", "+00:00"))
    catalog.commit("auction_baseline", "2026-10-19", rows,
        {"available_at": "2026-10-19T08:00:00+08:00"}, source="test",
        clock=at("2026-10-19T00:00:00Z"))
    catalog.commit("auction_baseline", "2026-10-20", rows,
        {"available_at": "2026-10-20T00:00:00+08:00"}, source="test",
        clock=at("2026-10-20T04:26:00Z"))
    providers = SliceProviders(catalog=catalog, redis=None, file_fallback=False)
    reader = SliceReader(providers.providers())
    result = reader.get("auction_baseline", trade_date="2026-10-20",
                        asof="2026-10-20T10:00:00+08:00")
    assert result.meta["status"] != "READY"
    assert result.meta["reason"] == "DATE_FALLBACK"
    assert result.meta["lineage"][0]["requested_date"] == "2026-10-20"
    assert result.meta["lineage"][0]["used_date"] == "2026-10-19"
    assert result.meta["lineage"][0]["available_at_trust"] == "MEASURED"
