from __future__ import annotations

import csv
import hashlib
import json

import pytest

from examples.run_real_f10_inventory import build_f10_inventory


FIELDS = ["股票代码", "股票简称", "总市值", "市盈率(pe)"]


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_f10_inventory_closes_file_identity_without_inventing_as_of(tmp_path):
    csv_path = tmp_path / "f10.csv"
    _write_csv(
        csv_path,
        [
            {"股票代码": "600519.SH", "股票简称": "贵州茅台", "总市值": "1", "市盈率(pe)": "2"},
            {"股票代码": "000004.SZ", "股票简称": "国华网安", "总市值": "", "市盈率(pe)": ""},
        ],
    )
    file_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    provenance_path = tmp_path / "f10_provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "file_sha256": file_sha,
                "rows": 2,
                "new_rows": 1,
                "identity_only_rows": ["000004.SZ"],
                "validation": {"name_count": 2, "duplicate_codes": 0},
            }
        ),
        encoding="utf-8",
    )

    result = build_f10_inventory(csv_path, provenance_path=provenance_path)

    assert result["inventory_valid"] is True
    assert result["accepted_for_identity"] is True
    assert result["core_ready"] is False
    assert result["core_blockers"] == ["financial_as_of_unknown", "financial_units_unknown"]
    assert result["row_count"] == 2
    assert result["identity_only_rows"] == 1
    assert result["authority_status"]["financial_as_of"] == "UNKNOWN"
    assert result["authority_status"]["financial_units"] == "UNKNOWN"
    assert result["provenance"]["consistent"] is True


def test_f10_inventory_fails_closed_on_duplicate_or_invalid_rows(tmp_path):
    csv_path = tmp_path / "f10.csv"
    _write_csv(
        csv_path,
        [
            {"股票代码": "600519.SH", "股票简称": "贵州茅台", "总市值": "1", "市盈率(pe)": "2"},
            {"股票代码": "600519.SH", "股票简称": "重复", "总市值": "1", "市盈率(pe)": "2"},
            {"股票代码": "bad", "股票简称": "坏代码", "总市值": "", "市盈率(pe)": ""},
        ],
    )

    result = build_f10_inventory(csv_path)

    assert result["inventory_valid"] is False
    assert result["accepted_for_identity"] is False
    assert result["duplicate_code_rows"] == 1
    assert result["invalid_code_rows"] == 1


def test_f10_inventory_rejects_missing_identity_columns(tmp_path):
    csv_path = tmp_path / "f10.csv"
    csv_path.write_text("code,name\n600519,贵州茅台\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        build_f10_inventory(csv_path)


def test_f10_inventory_rejects_duplicate_column_names(tmp_path):
    csv_path = tmp_path / "f10.csv"
    csv_path.write_text(
        "股票代码,股票简称,股票简称\n600519.SH,贵州茅台,重复列\n",
        encoding="utf-8",
    )

    result = build_f10_inventory(csv_path)

    assert result["duplicate_column_names"] == 1
    assert result["inventory_valid"] is False
    assert result["accepted_for_identity"] is False


def test_f10_inventory_marks_mismatched_provenance_unaccepted(tmp_path):
    csv_path = tmp_path / "f10.csv"
    _write_csv(
        csv_path,
        [{"股票代码": "600519.SH", "股票简称": "贵州茅台", "总市值": "1", "市盈率(pe)": "2"}],
    )
    provenance_path = tmp_path / "f10_provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "file_sha256": "wrong",
                "rows": 1,
                "identity_only_rows": [],
                "validation": {"name_count": 1, "duplicate_codes": 0},
            }
        ),
        encoding="utf-8",
    )

    result = build_f10_inventory(csv_path, provenance_path=provenance_path)

    assert result["provenance"]["consistent"] is False
    assert result["provenance"]["checks"]["file_sha256_matches"] is False
    assert result["accepted_for_identity"] is False
