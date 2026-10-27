"""C576: every daily_kline writer path produces a partition that passes the dataset contract.

No network. Partitions are produced by the real writer code (provider chain, Q2 snapshot, degraded
capture) from real captured upstream responses in tests/fixtures/real, then checked by the C570 checker.
"""
from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from conftest import read_real_artifact
from engine_core.data_pipeline.dataset_contract import check_partition
from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain, Q2FinalProvider
from engine_core.data_pipeline.storage import ArtifactCatalog, canonical_json
from engine_core.daily_kline_degraded import capture

FIXTURES = Path(__file__).parent / "fixtures" / "real"
DAY = "2026-10-23"
WENCAI_DAY = "2026-10-26"


def _failures(report):
    return {c["id"]: c["message"] for c in report.checks if c["status"] == "FAIL"}


def _assert_contract(root, day, **kw):
    report = check_partition(root, "daily_kline", day, **kw)
    assert report.passed, _failures(report)
    return report


def _seed_universe(root, day, symbols=None, from_day=DAY):
    manifest, rows = read_real_artifact("symbol_universe", from_day, "actual")
    if symbols is not None:
        keep = set(symbols)
        rows = [r for r in rows if str(r["symbol"]).zfill(6) in keep]
    ArtifactCatalog(root).commit("symbol_universe", day, rows, manifest,
                                 source=manifest.get("source", "baostock_query_all_stock"))


class _Pipeline:
    def __init__(self, redis):
        self.redis, self.keys = redis, []

    def hgetall(self, key):
        self.keys.append(key)
        return self.redis.rows[key]

    def execute(self):
        return [self.redis.rows[key] for key in self.keys]


class _Redis:
    def __init__(self, hashes):
        self.rows = {f"q2:{symbol}": item["value"] for symbol, item in hashes.items()}
        self.symbols = set(hashes)

    def smembers(self, key):
        return set(self.symbols)

    def pipeline(self, transaction=False):
        return _Pipeline(self)


def _q2_hashes():
    path = FIXTURES / "c526" / "redis_q2_hashes_20261023_first60.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _baostock_partition(root):
    _seed_universe(root, DAY)
    _, rows = read_real_artifact("daily_kline", DAY, "formal")
    chain = DailyKlineProviderChain(providers={"baostock": lambda d: {"rows": rows, "expected_count": None}},
                                    artifact_root=root)
    result = chain.fetch_chain(DAY, persist=True, now=DAY + "T18:00:00+08:00")
    assert result is not None
    return result.manifest


def _q2_snapshot_partition(root):
    hashes = _q2_hashes()
    _seed_universe(root, DAY, symbols=hashes)
    committed = Q2FinalProvider(_Redis(hashes)).snapshot(DAY, root, now=DAY + "T15:10:00+08:00")
    assert committed is not None
    return committed.manifest


def _degraded_partition(root):
    hashes = _q2_hashes()
    _seed_universe(root, DAY, symbols=hashes)
    return capture(DAY, artifact_root=root, redis_client=_Redis(hashes),
                   now=datetime.fromisoformat(DAY + "T15:10:00+08:00"), write=True)["manifest"]


def _wencai_partition(root):
    from test_daily_kline_wencai_date_validation import REQUEST_DATE, _captured_rows, _chain

    assert REQUEST_DATE == WENCAI_DAY
    rows, _ = _captured_rows()
    chain = _chain(root, rows)
    symbols = {str(r.get("股票代码", r.get("code", ""))).split(".")[0].zfill(6) for r in rows}
    from engine_core.wencai_fallback import _symbol
    _seed_universe(root, WENCAI_DAY, symbols={_symbol(r) for r in rows})
    result = chain.fetch_chain(WENCAI_DAY, now="2026-10-27T16:00:00+08:00", persist=True)
    assert result is not None
    return result.manifest


def test_baostock_chain_partition_passes_contract(tmp_path):
    manifest = _baostock_partition(tmp_path)
    assert manifest["provider"] == "baostock" and manifest["source"] == "baostock"
    assert "/source=baostock/" in manifest["manifest_uri"]
    assert manifest["provider_chain"] == ["baostock", "wencai", "q2_final"]
    assert manifest["degraded"] is False and manifest["business_status"] == "FORMAL"
    assert manifest["field_units"]["pct_chg"] == "bp"
    assert manifest["provider_attempts"][-1]["row_count"] == manifest["row_count"]
    quality = manifest["quality"]
    assert quality["dropped_outside_universe"] == manifest["quality_notes"]["universe_excluded_rows"]
    assert quality["dropped_duplicates"] == manifest["quality_notes"]["duplicate_rows"]
    _assert_contract(tmp_path, DAY)


def test_wencai_chain_partition_passes_contract(tmp_path):
    manifest = _wencai_partition(tmp_path)
    assert manifest["provider"] == "wencai" and manifest["source"] == "wencai"
    assert "/source=wencai/" in manifest["manifest_uri"]
    assert manifest["provider_chain"][0] == "baostock" and "wencai" in manifest["provider_chain"]
    assert manifest["business_status"] == "PROVISIONAL" and manifest["degraded"] is True
    assert all("row_count" in a for a in manifest["provider_attempts"])
    # C575 deliberately lets same-day wencai (PROVISIONAL) advance latest; design/24 section 2 / check E3 says
    # PROVISIONAL must not. That is a catalog-policy conflict, not a manifest defect: it is the only failure.
    report = check_partition(tmp_path, "daily_kline", WENCAI_DAY)
    assert set(_failures(report)) == {"E3"}, _failures(report)


def test_q2_snapshot_partition_passes_contract(tmp_path):
    manifest = _q2_snapshot_partition(tmp_path)
    assert manifest["provider"] == "q2_final" and manifest["source"] == "q2_final"
    assert "/source=q2_final/" in manifest["manifest_uri"]
    assert manifest["declared_source"] == "legacy_q2_final"
    assert manifest["degraded"] is True and manifest["provider_chain"] == ["q2_final"]
    assert manifest["provider_attempts"][0]["row_count"] == manifest["row_count"]
    assert manifest["quality"]["dropped_outside_universe"] is None  # snapshot path does not report drops
    # snapshot() advances latest on purpose (15:05 provisional close); see the wencai test for check E3
    assert set(_failures(check_partition(tmp_path, "daily_kline", DAY))) == {"E3"}


def test_degraded_capture_partition_passes_contract(tmp_path):
    manifest = _degraded_partition(tmp_path)
    # merged q2_final+wencai rows: the directory is the dominant provider, never the "a>b" label
    assert manifest["provider"] == "q2_final" and "/source=q2_final/" in manifest["manifest_uri"]
    assert manifest["provider_chain"] == ["q2_final", "wencai"]
    assert [a["row_count"] for a in manifest["provider_attempts"]] == [59, 0]
    assert manifest["degraded"] is True
    _assert_contract(tmp_path, DAY)


def test_commit_directory_is_provider_even_when_caller_passes_a_chain_label(tmp_path):
    _seed_universe(tmp_path, DAY)
    manifest, rows = read_real_artifact("daily_kline", DAY, "formal")
    committed = ArtifactCatalog(tmp_path).commit(
        "daily_kline", DAY, rows, {**manifest, "provider": "baostock", "provider_attempts": []},
        source="baostock>wencai>q2_final")
    assert "/source=baostock/" in committed["manifest_uri"] and committed["source"] == "baostock"


# ---- mutations: the checker (or writer test above) must fail ------------------------------------------
def _mutate(tmp_path, fn):
    root = tmp_path / "root"
    _baostock_partition(root)
    run = next((root / "daily_kline" / f"trade_date={DAY}").glob("source=*/sha256-*"))
    path = run / "manifest.json"
    m = json.loads(path.read_text())
    fn(m)
    path.write_bytes(canonical_json(m) + b"\n")
    return check_partition(root, "daily_kline", DAY)


@pytest.mark.parametrize("name,fn,expected", [
    ("no_provider_chain", lambda m: m.pop("provider_chain"), {"A3", "A3d"}),
    ("degraded_inconsistent", lambda m: m.update(degraded=True), {"A5"}),
    ("attempt_without_row_count", lambda m: [a.pop("row_count") for a in m["provider_attempts"]], {"A3c"}),
    ("no_drop_counts", lambda m: [m["quality"].pop("dropped_outside_universe"),
                                  m["quality"].pop("dropped_duplicates")], {"C4"}),
    ("no_field_units", lambda m: m.pop("field_units"), {"A3", "A11"}),
])
def test_mutated_manifest_fails_contract(tmp_path, name, fn, expected):
    report = _mutate(tmp_path, fn)
    assert not report.passed
    assert expected <= set(_failures(report)), (name, _failures(report))


def test_chain_label_source_directory_fails_contract(tmp_path):
    root = tmp_path / "root"
    _baostock_partition(root)
    old, new = "source=baostock/", "source=baostock>wencai>q2_final/"
    shutil.move(str(root / "daily_kline" / f"trade_date={DAY}" / "source=baostock"),
                str(root / "daily_kline" / f"trade_date={DAY}" / "source=baostock>wencai>q2_final"))
    for path in list(root.rglob("manifest.json")) + [root / "catalog.json", root / "daily_kline" / "latest.json"]:
        path.write_text(path.read_text().replace(old, new))
    assert "A2" in _failures(check_partition(root, "daily_kline", DAY))


# ---- legacy partitions: read-only tolerance ------------------------------------------------------------
def _legacy_partition(root):
    """A pre-C576 partition: raw percent layout, manifest without the contract fields."""
    from test_dataset_contract import build, rewrite_manifest, synthetic_rows, write_partition

    run = build(root, synthetic_rows())
    def strip(m):
        for key in ("provider_chain", "degraded", "field_units"):
            m.pop(key, None)
        m["source"] = "baostock>wencai>q2_final"
        for a in m["provider_attempts"]:
            a.pop("row_count", None)
        m["quality"].pop("dropped_outside_universe", None)
        m["quality"].pop("dropped_duplicates", None)
    rewrite_manifest(run, strip)
    return run


def test_legacy_tolerant_switch_waives_only_manifest_structure_of_legacy_partitions(tmp_path):
    _legacy_partition(tmp_path)
    strict = check_partition(tmp_path, "daily_kline", "2026-10-27")
    assert not strict.passed and {"A3", "A3c", "A3d", "A5", "A11", "C4"} <= set(_failures(strict))
    tolerant = check_partition(tmp_path, "daily_kline", "2026-10-27", legacy_tolerant=True)
    assert tolerant.passed, _failures(tolerant)
    assert tolerant.meta["legacy_waived"] and "A3" in tolerant.meta["legacy_waived"]
    assert any(c["status"] == "WARN" and c["message"].startswith("LEGACY-WAIVED") for c in tolerant.checks)


def test_legacy_tolerant_switch_does_not_relax_current_partitions(tmp_path):
    root = tmp_path / "root"
    _baostock_partition(root)
    run = next((root / "daily_kline" / f"trade_date={DAY}").glob("source=*/sha256-*"))
    path = run / "manifest.json"
    m = json.loads(path.read_text())
    m["degraded"] = True  # has provider_chain, so it is a current partition: no waiver
    path.write_bytes(canonical_json(m) + b"\n")
    assert "A5" in _failures(check_partition(root, "daily_kline", DAY, legacy_tolerant=True))
