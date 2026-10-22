import json
from datetime import datetime, timezone
from pathlib import Path

from engine_core.report_index import append_record, build_record, list_records


FIXTURES = Path(__file__).parent / "fixtures" / "report_index"


def _manifest(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_build_record_preserves_all_fixture_slices_and_explicit_unknowns():
    manifest = _manifest("auction_0926-001.manifest.json")
    record = build_record(manifest, out_dir="reports/2026-10-22/auction_0926-001",
                          completed_at="2026-10-22T01:26:01Z")
    assert set(record["input_versions"]) == set(manifest["slices"])
    assert record["date"] == manifest["trade_date"]
    assert record["stage"] == manifest["stage"]
    assert record["asof"] == manifest["asof"]
    assert record["checkpoint"] == (manifest["checkpoint"] or "UNKNOWN")
    assert record["report_json_bytes"] == manifest["report_json_bytes"]
    assert any(value["version"] == "UNKNOWN" or value["available_at"] == "UNKNOWN"
               for value in record["input_versions"].values())
    assert all(set(value) == {"version", "available_at", "status", "reason", "used_date"}
               for value in record["input_versions"].values())


def test_append_revision_order_and_idempotence(tmp_path):
    manifest = _manifest("auction_0926-001.manifest.json")
    first = build_record(manifest, out_dir="one", completed_at="2026-10-22T01:26:01Z")
    second_manifest = dict(manifest)
    second_manifest["slices"] = dict(manifest["slices"])
    name = next(iter(second_manifest["slices"]))
    second_manifest["slices"][name] = {**second_manifest["slices"][name], "version": "changed-v2"}
    second = build_record(second_manifest, out_dir="two", completed_at="2026-10-22T01:27:00Z")
    saved_one = append_record(tmp_path, first)
    saved_one_again = append_record(tmp_path, first)
    saved_two = append_record(tmp_path, second)
    assert saved_one["revision"] == saved_one_again["revision"] == 1
    assert saved_one["version"] == saved_one_again["version"]
    assert saved_one["authority"] == "LIVE"
    assert "source=report" in saved_one["storage_uri"]
    assert saved_two["revision"] == 2
    records = list_records(tmp_path, "2026-10-22", "auction")
    assert [row["completed_at"] for row in records] == [
        "2026-10-22T01:26:01Z", "2026-10-22T01:27:00Z"]
    assert [row["revision"] for row in records] == [1, 2]


def test_completed_at_is_injected_real_utc_and_catalog_commit_time_is_measured(tmp_path):
    manifest = _manifest("open_0932.manifest.json")
    before = datetime.now(timezone.utc)
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = build_record(manifest, out_dir="open", completed_at=completed_at)
    committed = append_record(tmp_path, record)
    after = datetime.now(timezone.utc)
    actual = datetime.fromisoformat(committed["available_at"].replace("Z", "+00:00"))
    assert before <= actual <= after
    assert completed_at != manifest["asof"]
    assert committed["available_at"].endswith("Z")
