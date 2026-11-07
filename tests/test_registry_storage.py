# DATA: spec (guardian timeline/schedule config)
"""CT-01 (design/29 §5): where a dataset lives is declared once in the registry; checks and readers follow the declaration."""
import copy
import json
from pathlib import Path

import pytest

from engine_core.guardian import commands
from engine_core.guardian.registry import load_registry, validate_registry

REGISTRY = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian"
                       / "datasets.registry.json").read_text(encoding="utf-8"))["items"]
KINDS = {"file", "catalog", "td_stable", "td_snapshot", "td_external", "redis_view", "unknown"}
ROLES = {"authority", "marker", "view", "mirror", "legacy"}


def storage_of(item):
    return item["storage"]


def test_every_registry_item_declares_where_it_is_stored():
    missing = [item["id"] for item in REGISTRY if "storage" not in item]
    assert missing == []


def test_every_declaration_has_a_valid_kind_role_and_name_and_a_single_authority():
    for item in REGISTRY:
        locations = storage_of(item)
        assert locations, item["id"]
        for location in locations:
            assert location["kind"] in KINDS and location["role"] in ROLES and location["name"], item["id"]
            if location["kind"] == "redis_view":
                assert isinstance(location.get("ttl_seconds"), int) and location["ttl_seconds"] > 0, item["id"]
        authorities = [loc for loc in locations if loc["role"] == "authority"]
        assert len(authorities) <= 1, item["id"]
        assert authorities or all(loc["role"] == "legacy" or loc["kind"] == "unknown" for loc in locations), item["id"]


def test_the_older_per_field_declarations_agree_with_storage():
    for item in REGISTRY:
        locations = storage_of(item)
        names = {(loc["kind"], loc["name"]) for loc in locations}
        catalog_dataset = item["artifact"].get("catalog_dataset")
        if catalog_dataset:
            assert ("catalog", catalog_dataset) in names, item["id"]
        sink = item.get("td_sink") or {}
        tables = {sink.get(key) for key in ("super_table", "stable", "table") if sink.get(key)}
        for table in tables:
            assert ("td_stable", table) in names, item["id"]
        ttl = item.get("redis_ttl_seconds")
        views = [loc for loc in locations if loc["kind"] == "redis_view"]
        if ttl is not None:
            assert views and ttl in {loc["ttl_seconds"] for loc in views}, item["id"]


def test_the_registry_loader_rejects_a_broken_declaration():
    base = copy.deepcopy(REGISTRY[1])
    validate_registry([base])
    for broken in (lambda s: s.update(kind="nowhere"), lambda s: s.update(role="boss"), lambda s: s.pop("name"),
                   lambda s: s.update(kind="redis_view", ttl_seconds=None)):
        item = copy.deepcopy(base)
        broken(item["storage"][0])
        with pytest.raises(ValueError, match="storage"):
            validate_registry([item])
    item = copy.deepcopy(base)
    item["storage"] = []
    with pytest.raises(ValueError, match="storage"):
        validate_registry([item])


def test_postcondition_snapshot_datasets_come_from_the_declaration_not_a_hardcoded_set():
    declared = {item["id"] for item in load_registry()
                if any(loc["kind"] == "td_snapshot" and loc["role"] == "authority" for loc in item["storage"])}
    assert declared == {"plate_members"}
    assert commands.snapshot_datasets() == declared


def test_a_dataset_whose_authority_moves_to_a_td_snapshot_gets_a_snapshot_check(monkeypatch, tmp_path):
    items = load_registry()
    moved = [copy.deepcopy(item) for item in items]
    target = next(item for item in moved if item["id"] == "limit_reasons")
    target["storage"] = [{"kind": "td_snapshot", "name": "limit_reasons", "role": "authority"}]
    monkeypatch.setattr(commands, "_registry_items", lambda: moved)
    assert "limit_reasons" in commands.snapshot_datasets()


def _checked_datasets():
    """Datasets some handler postcondition looks at (commands.validate_handler_postcondition)."""
    timeline = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian"
                           / "timeline.json").read_text(encoding="utf-8"))
    checked = {"symbol_universe", "daily_kline"}                    # premarket_data / postmarket_data / baostock_probe
    for name, function in timeline["functions"].items():
        if function["handler"] != "kaipan_fetch":
            continue
        triggers = [item for point in timeline["timeline"] for item in point["run"] if item["fn"] == name]
        for item in triggers or [{}]:
            args = {**function.get("args", {}), **item.get("args", {})}
            checked |= set(args.get("datasets") or ("limit_up_pool",))
    return checked


def test_every_dataset_a_postcondition_checks_is_stored_where_the_check_looks():
    by_id = {item["id"]: item for item in REGISTRY}
    snapshots = commands.snapshot_datasets()
    for dataset in sorted(_checked_datasets()):
        locations = by_id[dataset]["storage"]
        if dataset in snapshots:
            continue
        assert any(loc["kind"] == "catalog" and loc["role"] in ("authority", "marker") for loc in locations), (
            f"{dataset}: the postcondition reads catalog partitions but the declaration has none that are current")


def test_plate_amount_baseline_registry_matches_scheduled_event():
    schedule = json.loads((Path(__file__).resolve().parents[1] / "src/engine_core/guardian/schedule.json").read_text())
    item = next(item for item in REGISTRY if item["id"] == "plate_amount_baseline")
    event = next(event for event in schedule["events"] if event["event_id"] == item["guardian_event"])
    assert event["event_id"] == item["late_policy"]["trigger_event"] == "plate_amount_baseline_0815"
    assert event["args"]["item_id"] == item["id"] and event["phases"] == item["phases"]
    assert "DATE_FALLBACK" in item["degrade"][0] and "PARTIAL" in item["degrade"][0]
