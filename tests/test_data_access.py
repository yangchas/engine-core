from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from engine_core.data_pipeline import ArtifactCatalog, get
from engine_core.data_pipeline.registry import DATASETS
from engine_core.data_pipeline.access import select_version


FIXTURES = Path(__file__).parent / "fixtures" / "real"
FORMAL = FIXTURES / "core_artifacts/c522/daily_kline/2026-10-23/formal"


def _fixture():
    manifest = json.loads((FORMAL / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(FORMAL / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def _choice(version, path, *, gate="WARN", business="FORMAL", coverage=0.9,
            available_at="2026-10-23T10:00:00Z"):
    return {
        "entry": {"version": version, "manifest": path, "source": "baostock"},
        "manifest_path": path,
        "version": version, "source": "baostock", "gate_status": gate,
        "business_status": business, "coverage": coverage,
        "available_at": available_at,
        "manifest_data": {
            "gate": {"gate_status": gate}, "business_status": business,
            "quality": {"coverage": coverage}, "available_at": available_at,
        },
    }


def test_select_version_uses_as_of_quality_and_deterministic_ties():
    older = _choice("older", "/z/older.json", gate="FAIL", business="PROVISIONAL",
                    coverage=0.3395865237366003, available_at="2026-10-23T10:00:00Z")
    future = _choice("future", "/a/future.json", gate="WARN", business="FORMAL",
                     coverage=0.9998086124401914, available_at="2026-10-23T12:00:00Z")
    assert select_version([older, future], "2026-10-23T11:00:00Z") is older

    failed = _choice("failed", "/a/failed.json", gate="FAIL", coverage=1.0)
    warned = _choice("warned", "/z/warned.json", gate="WARN", business="PROVISIONAL",
                     coverage=0.2)
    assert select_version([failed, warned]) is warned

    formal = _choice("formal", "/z/formal.json", business="FORMAL", coverage=0.3395865237366003)
    provisional = _choice("provisional", "/a/provisional.json", business="PROVISIONAL",
                          coverage=0.9998086124401914)
    assert select_version([provisional, formal]) is formal

    low = _choice("1774-rows", "/daily/1774.json", coverage=0.3395865237366003)
    high = _choice("5216-rows", "/daily/5216.json", coverage=0.9980864906238041)
    assert select_version([low, high]) is high
    assert select_version([high, low]) is high

    lexical_a = _choice("a", "/catalog/a.json", coverage=0.9)
    lexical_b = _choice("b", "/catalog/b.json", coverage=0.9)
    assert select_version([lexical_b, lexical_a]) is lexical_a
    assert select_version([lexical_a, lexical_b]) is lexical_a


def test_get_reads_committed_real_rows_and_filters_as_of_and_tag(tmp_path):
    manifest, rows = _fixture()
    catalog = ArtifactCatalog(tmp_path / "core")
    for source, hour, tag in (("baostock", 10, "0925"), ("legacy_q2_final", 11, "0924")):
        version_manifest = dict(manifest, tag=tag)
        catalog.commit(
            "daily_kline", "2026-10-23", rows, version_manifest, source=source,
            clock=lambda hour=hour: datetime(2026, 10, 25, hour, tzinfo=timezone.utc),
        )

    by_time = get("daily_kline", trade_date="2026-10-23", as_of="2026-10-25T10:30:00Z",
                  view="raw", catalog=catalog)
    assert by_time.meta["available_at"] == "2026-10-25T10:00:00Z"
    assert by_time.meta["rows_total"] == len(rows)
    assert by_time.rows == rows
    assert all(key in by_time.meta for key in (
        "status", "version", "source", "adapter", "adapter_resolved_by", "business_status",
        "available_at", "gate_status", "coverage", "candidates", "rows_total", "rejected",
        "flag_counts", "unmapped_keys",
    ))
    assert any(item["eliminated_reason"] == "after_as_of" for item in by_time.meta["candidates"])

    by_tag = get("daily_kline", trade_date="2026-10-23", tag="0925", view="raw", catalog=catalog)
    assert by_tag.meta["version"] == by_time.meta["version"]
    assert any(item["eliminated_reason"] == "tag_mismatch" for item in by_tag.meta["candidates"])

    missing = get("daily_kline", trade_date="1900-01-01", view="raw", catalog=catalog)
    assert missing.rows == []
    assert missing.meta["status"] == "MISSING"


def test_get_canonicalizes_real_daily_kline_and_keeps_unregistered_rows(tmp_path, monkeypatch):
    manifest, rows = _fixture()
    catalog = ArtifactCatalog(tmp_path / "core")
    catalog.commit(
        "daily_kline", "2026-10-23", rows, manifest, source="baostock",
        clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )
    raw = get("daily_kline", trade_date="2026-10-23", view="raw", catalog=catalog)
    canonical = get("daily_kline", trade_date="2026-10-23", view="canonical", catalog=catalog)
    raw_first = next(row for row in raw.rows if row.get("symbol") == "000001")
    canonical_first = next(row for row in canonical.rows if row.get("symbol") == "000001")
    assert raw_first["close"] == 11.2
    assert canonical_first["close"] == 11200
    assert raw_first["pct_chg"] == -0.885
    assert canonical_first["pct_chg"] == -89
    assert canonical_first["volume"] == raw_first["volume"]

    monkeypatch.setitem(DATASETS, "daily_kline",
                        replace(DATASETS["daily_kline"], fields=(), source_aliases={}, checks=()))
    unregistered_catalog = ArtifactCatalog(tmp_path / "unregistered")
    unregistered_catalog.commit(
        "daily_kline", "2026-10-23", rows, manifest, source="baostock",
        clock=lambda: datetime(2026, 10, 25, tzinfo=timezone.utc),
    )
    unregistered = get("daily_kline", trade_date="2026-10-23", view="canonical",
                       catalog=unregistered_catalog)
    assert unregistered.rows == rows
    assert unregistered.meta["canonical"] == "UNREGISTERED"


def test_symbol_universe_locate_and_get_prefer_tier_zero_when_both_tiers_exist(tmp_path):
    from examples.run_data_jobs import _normalize_universe_rows

    raw_path = Path(__file__).parent / "fixtures" / "symbol_universe" / \
        "baostock_query_all_stock_20261016.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    rows = [dict(row, universe_asof="2026-10-22") for row in _normalize_universe_rows(raw)]
    catalog = ArtifactCatalog(tmp_path)
    primary = catalog.commit(
        "symbol_universe", "2026-10-26", rows,
        {"trade_date": "2026-10-26", "tier": 0, "degraded": False,
         "business_status": "PARTIAL", "quality": {"status": "READY", "coverage": 0.9}},
        source="baostock_query_all_stock",
        clock=lambda: datetime(2026, 10, 26, 1, tzinfo=timezone.utc))
    catalog.commit(
        "symbol_universe", "2026-10-26", rows,
        {"trade_date": "2026-10-26", "tier": 1, "degraded": True,
         "business_status": "PARTIAL", "quality": {"status": "DEGRADED", "coverage": 1.0}},
        source="carried_forward",
        clock=lambda: datetime(2026, 10, 26, 2, tzinfo=timezone.utc))

    located = json.loads(catalog.locate("symbol_universe", "2026-10-26").read_text())
    selected = get("symbol_universe", trade_date="2026-10-26", view="raw", root=tmp_path)

    assert located["tier"] == 0
    assert selected.meta["source"] == "baostock_query_all_stock"
    assert selected.meta["version"] == primary["version"]
