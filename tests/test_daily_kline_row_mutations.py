"""C579: mutating the canonical row layout (units, columns, code format) must fail the contract checker.

The mutated rows are committed through the real writer (ArtifactCatalog.commit), so sha256/row_count stay
consistent and the ONLY thing wrong is the layout under test.
"""
from __future__ import annotations

import copy
import gzip
import json

import pytest

from conftest import read_real_artifact
from engine_core.data_pipeline.dataset_contract import check_partition
from engine_core.data_pipeline.storage import ArtifactCatalog
from test_daily_kline_manifest_contract import _baostock_partition, _seed_universe
from test_dataset_contract import rewrite_manifest

DAY = "2026-10-23"


def _failures(report):
    return {c["id"]: c["message"] for c in report.checks if c["status"] == "FAIL"}


def _canonical_rows(tmp_path):
    manifest = _baostock_partition(tmp_path / "src")
    with gzip.open(manifest["rows_uri"], "rt", encoding="utf-8") as handle:
        return manifest, [json.loads(line) for line in handle if line.strip()]


def _commit(tmp_path, manifest, rows):
    root = tmp_path / "mut"
    _seed_universe(root, DAY)
    keep = {k: manifest[k] for k in ("schema_version", "dataset", "trade_date", "provider", "provider_chain",
                                     "provider_attempts", "as_of", "fetched_at", "available_at", "quality",
                                     "business_status") if k in manifest}
    committed = ArtifactCatalog(root).commit("daily_kline", DAY, rows, keep, source="baostock")
    return root, committed


def test_unmutated_canonical_partition_passes(tmp_path):
    manifest, rows = _canonical_rows(tmp_path)
    root, _ = _commit(tmp_path, manifest, rows)
    assert _failures(check_partition(root, "daily_kline", DAY)) == {}


def _apply(row_fn):
    def run(rows):
        out = copy.deepcopy(rows)
        for row in out:
            row_fn(row)
        return out
    return run


MUTATIONS = [
    ("pct_chg_basis_points",
     _apply(lambda r: r.update(pct_chg=None if r["pct_chg"] is None else int(round(r["pct_chg"] * 100)))),
     {"B5", "B7"}),
    ("turnover_pct_removed", _apply(lambda r: r.pop("turnover_pct")), {"B4b"}),
    ("suspended_removed", _apply(lambda r: r.pop("suspended")), {"B4b"}),
    ("volume_in_hands_nonint", _apply(lambda r: r.update(volume="x" if r["volume"] else r["volume"])), {"B5"}),
    ("turnover_pct_zero_filled", _apply(lambda r: r.update(turnover_pct=-1.0)), {"B7"}),
]


@pytest.mark.parametrize("name,fn,expected", MUTATIONS)
def test_row_layout_mutation_fails_checker(tmp_path, name, fn, expected):
    manifest, rows = _canonical_rows(tmp_path)
    root, _ = _commit(tmp_path, manifest, fn(rows))
    failed = set(_failures(check_partition(root, "daily_kline", DAY)))
    assert failed & expected, (name, failed)


def test_writer_marks_low_coverage_partition_partial_and_degraded(tmp_path):
    # design/24 (2026-10-28): PARTIAL counts as degraded and sets quality.partial; checker A5 accepts exactly that
    manifest, rows = _canonical_rows(tmp_path)
    root, committed = _commit(tmp_path, manifest, rows[:20])
    assert committed["business_status"] == "PARTIAL"
    assert committed["degraded"] is True and committed["quality"]["partial"] is True
    assert "A5" not in _failures(check_partition(root, "daily_kline", DAY))


def test_pct_chg_unit_declared_as_basis_points_fails_checker(tmp_path):
    manifest, rows = _canonical_rows(tmp_path)
    root, committed = _commit(tmp_path, manifest, rows)
    run = next((root / "daily_kline" / f"trade_date={DAY}").glob("source=*/sha256-*"))
    rewrite_manifest(run, lambda m: m["field_units"].update(pct_chg="bp"))
    assert "A11" in _failures(check_partition(root, "daily_kline", DAY))


def test_volume_unit_declared_as_hands_fails_checker(tmp_path):
    manifest, rows = _canonical_rows(tmp_path)
    root, _ = _commit(tmp_path, manifest, rows)
    run = next((root / "daily_kline" / f"trade_date={DAY}").glob("source=*/sha256-*"))
    rewrite_manifest(run, lambda m: m["field_units"].update(volume="hands"))
    assert "A11" in _failures(check_partition(root, "daily_kline", DAY))


def test_code_format_change_is_caught_by_the_comparison_not_the_checker(tmp_path):
    # the checker only requires `code` to be a nullable string; the layout guard for its format is the comparator
    from examples.compare_daily_kline_partitions import ComparisonError, compare_partitions

    manifest, rows = _canonical_rows(tmp_path)
    ref = read_real_artifact("daily_kline", DAY, "formal")
    bad = copy.deepcopy(rows)
    for row in bad:
        row["code"] = row["symbol"]
    with pytest.raises(ComparisonError, match="code_or_symbol_format"):
        compare_partitions(ref[0], ref[1], manifest, bad)
