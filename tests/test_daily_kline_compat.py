"""C579: the new daily_kline chain output is column-for-column compatible with the live partition layout.

Offline: the reference is the real captured Baostock FORMAL partition of 2026-10-23 (same layout as the live
core-data partitions: percent pct_chg, turnover_pct, code sz.000001, volume in shares); the candidates are produced
by the real writer code. Mutations of the candidate (pct_chg back to basis points, turnover_pct removed, code
format changed, volume in hands) must make the comparison fail.
"""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest

from conftest import read_real_artifact
from examples.compare_daily_kline_partitions import ComparisonError, compare_partitions
from engine_core.data_pipeline.daily_kline_schema import CANONICAL_FIELD_UNITS, CANONICAL_ROW_FIELDS
from test_daily_kline_manifest_contract import _baostock_partition, _q2_snapshot_partition

DAY = "2026-10-23"
SPEC = Path(__file__).parents[1] / "src/engine_core/data_pipeline/dataset_specs/daily_kline.json"


def _new_rows(manifest):
    with gzip.open(manifest["rows_uri"], "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@pytest.fixture()
def baostock_pair(tmp_path):
    reference = read_real_artifact("daily_kline", DAY, "formal")
    manifest = _baostock_partition(tmp_path)
    return reference, (manifest, _new_rows(manifest))


def test_baostock_tier_new_partition_matches_reference_column_for_column(baostock_pair):
    (ref_manifest, ref_rows), (new_manifest, new_rows) = baostock_pair
    result = compare_partitions(ref_manifest, ref_rows, new_manifest, new_rows)
    assert result["status"] == "PASS" and result["total_mismatched"] == 0
    assert result["common_symbols"] == len(ref_rows) == len(new_rows)
    assert result["left_only"] == result["right_only"] == 0
    assert set(CANONICAL_ROW_FIELDS) <= set(new_rows[0])
    assert result["right"]["unit_sense"]["pct_chg_percent_fraction"] == 1.0
    assert result["right"]["unit_sense"]["volume_unit"] == "shares"
    # only columns that no consumer reads may be absent, and they are listed, not hidden
    assert set(result["columns_only_in_left_waived"]) <= {"turnover", "mismatch", "universe_version"}
    for field in ("open", "high", "low", "close", "pre_close", "pct_chg", "turnover_pct", "amount_yuan", "volume"):
        assert result["value_comparison"][field]["mismatched"] == 0


def test_q2_tier_matches_baostock_in_units_and_every_value_but_high_low(tmp_path):
    reference = read_real_artifact("daily_kline", DAY, "formal")
    manifest = _q2_snapshot_partition(tmp_path)
    rows = _new_rows(manifest)
    assert set(CANONICAL_ROW_FIELDS) <= set(rows[0])
    result = compare_partitions(reference[0], reference[1], manifest, rows, skip_fields=("high", "low"))
    assert result["status"] == "PASS" and result["total_mismatched"] == 0 and result["common_symbols"] == len(rows)
    assert result["value_comparison"]["volume"]["compared"] == len(rows)  # hands -> shares conversion agrees
    assert result["value_comparison"]["turnover_pct"]["compared"] == 0  # q2 has no turnover: null, never 0
    assert all(row["turnover_pct"] is None and row["suspended"] is None for row in rows)
    # q2 mx/mn are not Baostock's session high/low: a real source difference, so the unfiltered comparison reports it
    full = compare_partitions(reference[0], reference[1], manifest, rows)
    assert full["status"] == "FAIL_VALUES"
    assert {f for f, m in full["value_comparison"].items() if m["mismatched"]} <= {"high", "low"}


def _mutate(rows, fn):
    out = copy.deepcopy(rows)
    for row in out:
        fn(row)
    return out


def _fails(pair, rows, match, manifest=None):
    (ref_manifest, ref_rows), (new_manifest, _) = pair
    with pytest.raises(ComparisonError, match=match):
        compare_partitions(ref_manifest, ref_rows, manifest or new_manifest, rows)


def test_mutation_pct_chg_back_to_basis_points_fails(baostock_pair):
    rows = _mutate(baostock_pair[1][1], lambda r: r.update(pct_chg=None if r["pct_chg"] is None
                                                           else int(round(r["pct_chg"] * 100))))
    _fails(baostock_pair, rows, "pct_chg_not_percent")


def test_mutation_basis_point_units_in_manifest_fails(baostock_pair):
    manifest = {**baostock_pair[1][0], "field_units": {**CANONICAL_FIELD_UNITS, "pct_chg": "bp"}}
    _fails(baostock_pair, baostock_pair[1][1], "right_field_units_invalid", manifest)


def test_mutation_turnover_pct_column_removed_fails(baostock_pair):
    rows = _mutate(baostock_pair[1][1], lambda r: r.pop("turnover_pct"))
    _fails(baostock_pair, rows, "right_field_set_mismatch:turnover_pct|right_missing_columns:turnover_pct")


def test_mutation_code_format_changed_fails(baostock_pair):
    rows = _mutate(baostock_pair[1][1], lambda r: r.update(code=r["symbol"]))
    _fails(baostock_pair, rows, "right_code_or_symbol_format")


def test_mutation_symbol_with_exchange_prefix_fails(baostock_pair):
    rows = _mutate(baostock_pair[1][1], lambda r: r.update(symbol=r["code"]))
    _fails(baostock_pair, rows, "right_code_or_symbol_format")


def test_mutation_volume_in_hands_fails(baostock_pair):
    rows = _mutate(baostock_pair[1][1], lambda r: r.update(volume=None if r["volume"] is None
                                                           else r["volume"] / 100.0))
    _fails(baostock_pair, rows, "right_volume_not_shares")


def test_mutation_value_drift_is_counted(baostock_pair):
    (ref_manifest, ref_rows), (new_manifest, new_rows) = baostock_pair
    rows = copy.deepcopy(new_rows)
    rows[0]["close"] = rows[0]["close"] + 1.0
    result = compare_partitions(ref_manifest, ref_rows, new_manifest, rows)
    assert result["status"] == "FAIL_VALUES" and result["value_comparison"]["close"]["mismatched"] == 1


def test_spec_and_schema_describe_the_same_layout():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    columns = spec["columns"]
    assert set(CANONICAL_ROW_FIELDS) <= set(columns)
    for field, unit in CANONICAL_FIELD_UNITS.items():
        assert columns[field]["unit"] == unit, field
    assert columns["pct_chg"]["type"] == "float" and columns["turnover_pct"]["type"] == "float"
