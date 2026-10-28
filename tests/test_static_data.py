"""Static snapshot builders are exercised only with captured real source data."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from engine_core.data_pipeline.static_data import (
    build_plate_members,
    build_plate_registry,
    build_stock_static,
    retry_delay_seconds,
    static_refresh_decision,
)
from engine_core.data_pipeline.dataset_contract import check_partition, load_spec
from engine_core.data_pipeline.storage import ArtifactCatalog

REAL = Path(__file__).parent / "fixtures" / "real"
CORE = REAL / "core_artifacts" / "c594"


def _core_rows(dataset: str, day: str, label: str = "current") -> list[dict]:
    path = CORE / dataset / day / label / "rows.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def test_real_static_builders_keep_rows_and_report_identity_and_source_gaps(tmp_path):
    as_of = "2026-10-29T04:18:56+08:00"
    registry_source = _core_rows("plate_registry", "2026-10-13")
    members_source = _core_rows("plate_members", "2026-10-28")
    universe = _core_rows("symbol_universe", "2026-10-28", "baostock_query_all_stock")
    basic_path = next(REAL.glob("baostock_stock_basic_*.json"))
    basic = json.loads(basic_path.read_text(encoding="utf-8"))

    registry = build_plate_registry(
        registry_source, trade_date="2026-10-13", as_of=as_of,
        source_dates={"plate_registry": "2026-10-13"},
    )
    assert len(registry.rows) == 709
    assert all(row["date"] == "2026-10-13" for row in registry.rows)
    assert len({row["plate_code"] for row in registry.rows}) == 709

    changed_members = [dict(row) for row in members_source]
    changed_members[0]["plate_code"] = "899999"
    members = build_plate_members(
        changed_members, registry.rows, trade_date="2026-10-28", as_of=as_of,
        source_dates={"plate_members": "2026-10-28"},
    )
    assert len(members.rows) == len(members_source)
    assert any(row["plate_code"] == "899999" and row["date"] == "2026-10-28"
               for row in members.rows)
    assert members.manifest["quality"]["unregistered_plate_code_rows"] == 1

    stock = build_stock_static(
        universe, basic, trade_date="2026-10-29", as_of=as_of,
        source_dates={"symbol_universe": "2026-10-28", "baostock_stock_basic": None},
    )
    assert len(stock.rows) == len(universe)
    assert all(row["date"] == "2026-10-29" for row in stock.rows)
    assert all("exchange" in row and "market_board" in row for row in stock.rows)
    assert stock.manifest["source_dates"]["baostock_stock_basic"] is None
    assert stock.manifest["quality"]["missing_baostock_basic_count"] > 0

    # C570 contract checker against temporary catalog writes built only from captured real inputs.
    catalog = ArtifactCatalog(tmp_path)
    for partition in (registry, members, stock):
        trade_date = partition.manifest["trade_date"]
        committed = catalog.commit(
            partition.manifest["dataset"], trade_date, partition.rows, partition.manifest,
            source=partition.manifest["provider"],
            clock=lambda: datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc),
        )
        report = check_partition(tmp_path, partition.manifest["dataset"], trade_date,
                                 version=committed["version"], spec=load_spec(partition.manifest["dataset"]))
        assert report.passed, report.summary()


def test_newer_real_stock_static_provisional_snapshot_advances_latest(tmp_path):
    old_manifest_path = CORE / "stock_static" / "2026-10-14" / "current" / "manifest.json"
    new_manifest_path = CORE / "stock_static" / "2026-10-29" / "c573_guardian" / "manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    new_manifest = json.loads(new_manifest_path.read_text(encoding="utf-8"))
    assert new_manifest["business_status"] == "PROVISIONAL"

    catalog = ArtifactCatalog(tmp_path)
    for day, manifest, label in (
        ("2026-10-14", old_manifest, "current"),
        ("2026-10-29", new_manifest, "c573_guardian"),
    ):
        committed = catalog.commit(
            "stock_static", day, _core_rows("stock_static", day, label), manifest,
            source=manifest.get("source") or "legacy_snapshot",
            clock=lambda: datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc),
        )
        assert committed["trade_date"] == day

    latest = catalog.read()["datasets"]["stock_static"]["latest"]
    assert latest["trade_date"] == "2026-10-29"
    report = check_partition(
        tmp_path, "stock_static", "2026-10-29", version=latest["version"],
        spec=load_spec("stock_static"),
    )
    assert report.passed, report.summary()


def test_static_refresh_policy_fake_clock_and_mutations():
    now = datetime(2026, 10, 29, 8, 55, tzinfo=timezone.utc)
    current = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
    due = static_refresh_decision(
        now=now, trigger_time="08:50", last_available_at=current,
        max_age_days=8, weekly_days=7, universe_symbols={"000001", "000002"},
        snapshot_symbols={"000001"},
    )
    assert due["refresh"] and due["catch_up"] and due["new_symbol_count"] == 1
    assert due["stale_alert"]
    bad_period = static_refresh_decision(
        now=now, trigger_time="08:50", last_available_at=current,
        max_age_days=30, weekly_days=30, universe_symbols={"000001"},
        snapshot_symbols={"000001"},
    )
    assert not bad_period["refresh"] and not bad_period["stale_alert"]

    not_due = static_refresh_decision(
        now=datetime(2026, 10, 29, 8, 40, tzinfo=timezone.utc),
        trigger_time="08:50", last_available_at=datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc),
        max_age_days=8, weekly_days=7, universe_symbols={"000001"},
        snapshot_symbols={"000001"},
    )
    assert not not_due["refresh"] and not not_due["stale_alert"]
    assert [retry_delay_seconds(i) for i in (1, 2, 3, 5)] == [60, 120, 240, 600]

    # Mutations must make the corresponding guard fail rather than silently pass.
    with pytest.raises(ValueError, match="identity"):
        bad = [dict(row) for row in _core_rows("plate_members", "2026-10-28")]
        bad[0]["plate_code"] = ""
        build_plate_members(
            bad, ["801001"], trade_date="2026-10-29", as_of="2026-10-29T08:55:00+08:00",
            source_dates={"plate_members": "2026-10-28"},
        )


def test_static_refresh_schedule_and_name_map_policy_are_explicit():
    root = Path(__file__).resolve().parents[1]
    schedule = json.loads((root / "src/engine_core/guardian/schedule.json").read_text())
    registry = json.loads((root / "src/engine_core/guardian/datasets.registry.json").read_text())
    event = next(row for row in schedule["events"]
                 if row["event_id"] == "static_data_refresh_0850")
    cfg = event["args"]["static_data_refresh"]
    assert event["handler"] == "premarket_data"
    assert event["at"] == "08:50:00" and event["late_until"] == "15:00:00"
    assert event["on_start"] == "reconcile"
    assert schedule["profiles"][event["profile"]]["ladder"][0] == {
        "name": "legacy", "attempts": 3, "timeout": "300s", "backoff": "60s*2",
        "backoff_cap": "10m", "until": "late_until", "budget_enabled": False,
    }
    assert cfg["datasets"] == ["plate_registry", "plate_members", "stock_static"]
    assert cfg["weekly_days"] == 7 and cfg["max_age_days"] == 8
    assert cfg["kaipan_max_requests"] == 400
    assert cfg["fallbacks"] == []
    items = {row["id"]: row for row in registry["items"]}
    for dataset in ("plate_registry", "plate_members", "stock_static", "name_map"):
        row = items[dataset]
        assert "UNKNOWN" not in row["cadence"]
        assert row["guardian_event"] == "static_data_refresh_0850"
        assert row["late_policy"]["mode"] == "fetch_now"
        assert row["late_policy"]["valid_until"] == "15:00:00"
