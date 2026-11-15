from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.hot_plates_builder import build_hot_plates
from engine_core.data_pipeline import dataset_contract as contract
from engine_core.data_pipeline.storage import ArtifactCatalog, canonical_json
from engine_core.data_pipeline.jobs import JobRunner


FIXTURE = Path(__file__).parent / "fixtures" / "real" / "kaipan_hot_plates_2026-10-23_1441.json"


def _request(response):
    return {
        "trade_date": "2026-10-23",
        "captured_at": "2026-10-23T14:41:16+08:00",
        "fetched_at": "2026-10-23T14:41:16+08:00",
        "response": response,
        "source": "kaipan_getHisPlates",
        "page_size": 50,
        "event_id": "kaipan_history_20261023",
    }


def test_builder_keeps_confirmed_positions_and_unknown_raw_fields():
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))

    part = build_hot_plates(_request(response))

    assert len(part.rows) == 50
    first = part.rows[0]
    raw = response["list"][0]
    assert first["plate_code"] == raw[0]
    assert first["name"] == raw[1]
    assert first["strength"] == raw[2]
    assert first["change_pct"] == raw[3]
    assert first["amount_yuan"] == raw[5]
    assert first["raw_fields"] == raw
    assert first["date"] == first["trade_date"] == "2026-10-23"
    assert first["event_id"] == "kaipan_history_20261023"
    assert first["captured_at"] == first["available_at"] == "2026-10-23T14:41:16+08:00"
    assert part.manifest["source_dates"] == {"kaipan_getHisPlates": "2026-10-23"}
    assert part.manifest["response_total"] == 240
    assert part.manifest["expected_count"] == 240
    assert part.manifest["coverage_denominator"] == 240
    assert part.manifest["coverage"] == 50 / 240
    assert part.manifest["partial"] is True
    assert part.manifest["expected_basis"] == "response_total"
    assert part.manifest["field_units"]["strength"] == (
        "integer; scale and normalization UNKNOWN; raw positional field [2]")


@pytest.mark.parametrize("day", [None, ["2026-10-22"]])
def test_builder_requires_matching_response_day_evidence(day):
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if day is None:
        response.pop("Day")
    else:
        response["Day"] = day

    with pytest.raises(ValueError, match="kaipan_hot_plates_response_date"):
        build_hot_plates(_request(response))


class _Redis:
    def __init__(self):
        self.values = {}
    def get(self, key):
        return self.values.get(key)
    def set(self, key, value, ex=None):
        self.values[key] = value
    def delete(self, key):
        self.values.pop(key, None)


@pytest.fixture
def redis_runner(tmp_path, monkeypatch):
    from engine_core.data_pipeline import jobs
    monkeypatch.setenv("CORE_DATA_ROOT", str(Path.home() / "core-data"))
    monkeypatch.setattr(jobs.time, "time", lambda: 1792764000.0)
    redis = _Redis()
    return JobRunner(artifact_root=tmp_path, redis=redis), redis


def test_hot_plates_partition_passes_dataset_contract(tmp_path, redis_runner):
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))
    built = build_hot_plates(_request(response))
    runner, redis = redis_runner
    result = runner.run("hot_plates", "2026-10-23",
        fetch=lambda _day: {**built.manifest, "rows": built.rows, "request_count": 1},
        now="2026-10-23T14:41:16+08:00", as_of="2026-10-23",
        source="kaipan_getHisPlates", publish=True)
    view = json.loads(redis.values[f"core:cache:data:hot_plates:2026-10-23:{result.manifest['version']}"])
    rows = view["rows"]
    raw = b"\n".join(canonical_json(row) for row in rows) + b"\n"
    report = contract.Report("hot_plates", "2026-10-23", tmp_path)
    spec = contract.apply_provider_overrides(contract.load_spec("hot_plates"), view.get("provider"))
    contract._check_manifest(report, "hot_plates", "2026-10-23", view, view, rows, raw, spec)
    contract._check_rows(report, "2026-10-23", view, rows, spec)
    contract._check_coverage(report, ArtifactCatalog(tmp_path), "2026-10-23", view, rows, spec)
    contract._check_idempotency(report, view, rows, raw, spec)
    assert report.passed, report.summary()
    assert report.stats["response_total_coverage"]["denominator"] == 240
    assert view["available_at"] == view["captured_at"]
    assert not (tmp_path / "catalog.json").exists()


def test_each_capture_appends_a_distinct_same_day_version(tmp_path, redis_runner):
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runner, redis = redis_runner
    versions = []
    for captured_at, event_id in (
        ("2026-10-23T09:16:15+08:00", "kaipan_hot_091610"),
        ("2026-10-23T09:21:15+08:00", "kaipan_hot_092110"),
    ):
        built = build_hot_plates({**_request(response), "captured_at": captured_at,
                                  "event_id": event_id})
        result = runner.run("hot_plates", "2026-10-23",
            fetch=lambda _day, _ctx=None, built=built: {**built.manifest, "rows": built.rows,
                                                         "request_count": 1},
            now=captured_at, as_of="2026-10-23", source="kaipan_getHisPlates", publish=True)
        versions.append(result.manifest["version"])
    prefix = "core:cache:data:hot_plates:2026-10-23"
    assert versions[0] != versions[1]
    assert all(json.loads(redis.values[f"{prefix}:{version}"])["version"] == version for version in versions)
    assert json.loads(redis.values[prefix])["version"] == versions[-1]
    assert not (tmp_path / "catalog.json").exists()
