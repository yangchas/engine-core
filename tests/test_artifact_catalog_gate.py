import gzip
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from engine_core.data_pipeline.registry import DATASETS, get_dataset
from engine_core.data_pipeline.storage import ArtifactCatalog, canonical_json


FIXTURES = Path(__file__).parent / "fixtures" / "real"
DAILY_KLINE = FIXTURES / "core_artifacts/c522/daily_kline/2026-10-23/formal"


def _daily_kline_fixture():
    manifest = json.loads((DAILY_KLINE / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(DAILY_KLINE / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def test_daily_kline_commit_records_observational_gate_for_real_rows(tmp_path):
    source_manifest, rows = _daily_kline_fixture()
    catalog = ArtifactCatalog(tmp_path / "core")
    result = catalog.commit(
        "daily_kline", "2026-10-23", rows, source_manifest,
        source="baostock", clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )

    assert result["gate"]["schema_version"] == 1
    assert result["gate"]["adapter"] == "baostock"
    assert result["gate"]["adapter_resolved_by"] == "provider_attempts"
    assert result["gate"]["mode"] == "observe"
    assert result["gate"]["rows"] == len(rows) == 5224
    assert result["gate"]["gate_status"] in {"OK", "WARN"}
    rows_path = Path(result["rows_uri"])
    payload = b"\n".join(canonical_json(dict(row)) for row in rows) + b"\n"
    assert hashlib.sha256(rows_path.read_bytes()).hexdigest() == hashlib.sha256(
        gzip.compress(payload, mtime=0)
    ).hexdigest()


def test_bad_value_in_real_daily_row_is_flagged_and_counted(tmp_path):
    _, fixture_rows = _daily_kline_fixture()
    row = dict(fixture_rows[0])
    row["close"] = "not-a-price"
    result = ArtifactCatalog(tmp_path / "core").commit(
        "daily_kline", "2026-10-23", [row], {}, source="baostock",
        clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )

    assert result["gate"]["flags_by_field"]["close"] == 1
    assert result["gate"]["flagged_rows"] == 1
    assert result["gate"]["gate_status"] in {"WARN", "FAIL"}


def test_unresolved_adapter_fails_gate(tmp_path):
    _, fixture_rows = _daily_kline_fixture()
    result = ArtifactCatalog(tmp_path / "core").commit(
        "daily_kline", "2026-10-23", fixture_rows[:2], {}, source="x",
        clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )

    assert result["gate"]["adapter"] is None
    assert result["gate"]["adapter_resolved_by"] == "unresolved"
    assert result["gate"]["gate_status"] == "FAIL"


def test_enforce_blocks_failed_latest_but_observe_does_not(tmp_path, monkeypatch):
    _, fixture_rows = _daily_kline_fixture()
    bad_row = dict(fixture_rows[0])
    bad_row["close"] = "not-a-price"
    commit_time = lambda: datetime(2026, 10, 25, tzinfo=timezone.utc)

    observed = ArtifactCatalog(tmp_path / "observe")
    observed.commit("daily_kline", "2026-10-23", [bad_row], {}, source="baostock",
                    clock=commit_time)
    observed_latest = observed.read()["datasets"]["daily_kline"]["latest"]
    assert observed_latest["trade_date"] == "2026-10-23"

    monkeypatch.setitem(DATASETS, "daily_kline", replace(get_dataset("daily_kline"), gate_mode="enforce"))
    enforced = ArtifactCatalog(tmp_path / "enforce")
    manifest = enforced.commit("daily_kline", "2026-10-23", [bad_row], {}, source="baostock",
                               clock=commit_time)
    catalog = enforced.read()
    version = catalog["datasets"]["daily_kline"]["dates"]["2026-10-23"][0]
    assert manifest["gate"]["gate_status"] == "FAIL"
    assert version["gate_status"] == "FAIL"
    assert catalog["datasets"]["daily_kline"]["latest"] is None


def test_empty_registry_fields_keep_parent_commit_bytes(tmp_path):
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    def response(level):
        path = FIXTURES / f"kaipan_pool_2026-10-22_L{level}_1444.json"
        return json.loads(path.read_text(encoding="utf-8"))

    class ReplayKaipan:
        def get_his_bans(self, trade_date, level):
            return response(level)

    rows = KaipanProvider(
        ReplayKaipan(), max_requests=2, min_interval_seconds=0.2, sleep_fn=lambda _: None,
    ).limit_up_pool("2026-10-22", max_levels=2)["rows"]
    assert rows

    root = tmp_path / "core"
    catalog = ArtifactCatalog(root)
    result = catalog.commit(
        "limit_up_pool", "2026-10-22", rows, {}, source="kaipan",
        clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )
    assert "gate" not in result
    assert "gate_status" not in catalog.read()["datasets"]["limit_up_pool"]["dates"]["2026-10-22"][0]

    payload = b"\n".join(canonical_json(dict(row)) for row in rows) + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    version = f"sha256-{digest[:16]}"
    run_dir = root / "limit_up_pool" / "trade_date=2026-10-22" / "source=kaipan" / version
    data_path = run_dir / "rows.jsonl.gz"
    manifest_path = run_dir / "manifest.json"
    expected_manifest = {
        "committed_at": "2026-10-25T00:00:00Z",
        "available_at": "2026-10-25T00:00:00Z",
        "authority": "LIVE",
        "captured_at": None,
        "business_date": "2026-10-22",
        "trade_date": "2026-10-22",
        "source_asof": "UNKNOWN",
        "version": version,
        "sha256": digest,
        "row_count": len(rows),
        "storage_backend": "artifact",
        "storage_uri": str(run_dir),
        "rows_uri": str(data_path),
        "manifest_uri": str(manifest_path),
        "storage_format": "jsonl.gz",
    }
    assert data_path.read_bytes() == gzip.compress(payload, mtime=0)
    assert manifest_path.read_bytes() == canonical_json(expected_manifest) + b"\n"
