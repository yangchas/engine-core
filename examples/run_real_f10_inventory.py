"""Create a read-only inventory for a static F10 CSV snapshot.

The command establishes file identity and structural facts only.  It does not
infer a financial as-of date or units, and it never calls Redis or a F10
service cache writer.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "RealF10InventoryV1"
CODE_COLUMN = "股票代码"
NAME_COLUMN = "股票简称"
CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _identity_only_count(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _provenance_summary(
    path: Path,
    *,
    file_sha256: str,
    row_count: int,
    identity_only_rows: int,
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("F10 provenance must be a JSON object")

    declared_identity_rows = _identity_only_count(payload.get("identity_only_rows"))
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    declared_new_rows = payload.get("new_rows")
    checks = {
        "file_sha256_matches": payload.get("file_sha256") == file_sha256,
        "rows_match": payload.get("rows") == row_count,
        "name_count_match": validation.get("name_count") == row_count,
        "duplicate_codes_zero": validation.get("duplicate_codes") == 0,
        "identity_only_count_match": (
            declared_identity_rows is not None
            and declared_identity_rows == identity_only_rows
        ),
        "new_plus_identity_rows_match": (
            isinstance(declared_new_rows, int)
            and not isinstance(declared_new_rows, bool)
            and declared_identity_rows is not None
            and declared_new_rows + declared_identity_rows == row_count
        ),
    }
    return {
        "file_name": path.name,
        "file_sha256": _sha256_file(path)[1],
        "declared_file_sha256": payload.get("file_sha256"),
        "declared_rows": payload.get("rows"),
        "declared_new_rows": declared_new_rows,
        "declared_identity_only_rows": declared_identity_rows,
        "validation_name_count": validation.get("name_count"),
        "validation_duplicate_codes": validation.get("duplicate_codes"),
        "checks": checks,
        "consistent": all(checks.values()),
    }


def build_f10_inventory(
    csv_path: Path,
    *,
    provenance_path: Path | None = None,
) -> dict[str, Any]:
    """Read a static F10 file and return structural, content-addressed facts."""

    file_size, file_sha256 = _sha256_file(csv_path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        field_names = list(reader.fieldnames or [])
        duplicate_column_names = len(field_names) - len(set(field_names))
        missing_required_columns = [
            column for column in (CODE_COLUMN, NAME_COLUMN) if column not in field_names
        ]
        if missing_required_columns:
            raise ValueError(
                "F10 CSV is missing required columns: "
                + ", ".join(missing_required_columns)
            )

        row_count = 0
        malformed_rows = 0
        invalid_code_rows = 0
        missing_name_rows = 0
        identity_only_rows = 0
        codes: list[str] = []
        financial_columns = [
            column for column in field_names if column not in {CODE_COLUMN, NAME_COLUMN}
        ]
        for row in reader:
            row_count += 1
            if None in row:
                malformed_rows += 1
            code = str(row.get(CODE_COLUMN) or "").strip()
            if CODE_PATTERN.fullmatch(code) is None:
                invalid_code_rows += 1
            else:
                codes.append(code)
            if _blank(row.get(NAME_COLUMN)):
                missing_name_rows += 1
            if all(_blank(row.get(column)) for column in financial_columns):
                identity_only_rows += 1

    duplicate_code_rows = len(codes) - len(set(codes))
    inventory_valid = bool(
        row_count
        and not malformed_rows
        and not invalid_code_rows
        and not duplicate_code_rows
        and not duplicate_column_names
        and not missing_name_rows
    )
    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "file_name": csv_path.name,
        "file_size_bytes": file_size,
        "file_sha256": file_sha256,
        "encoding": "UTF-8",
        "field_names": field_names,
        "duplicate_column_names": duplicate_column_names,
        "row_count": row_count,
        "unique_valid_code_count": len(set(codes)),
        "code_format": "six_digits.exchange_suffix(SH|SZ|BJ)",
        "malformed_rows": malformed_rows,
        "invalid_code_rows": invalid_code_rows,
        "duplicate_code_rows": duplicate_code_rows,
        "missing_name_rows": missing_name_rows,
        "financial_column_count": len(financial_columns),
        "identity_only_rows": identity_only_rows,
        "inventory_valid": inventory_valid,
        "provenance": (
            _provenance_summary(
                provenance_path,
                file_sha256=file_sha256,
                row_count=row_count,
                identity_only_rows=identity_only_rows,
            )
            if provenance_path
            else None
        ),
        "authority_status": {
            "file_identity": "observed",
            "financial_as_of": "UNKNOWN",
            "financial_units": "UNKNOWN",
            "redis_side_effects": "not_invoked",
        },
        "side_effect_boundary": "local CSV/provenance read only; no Redis/TD/F10 service writer",
    }
    result["accepted_for_identity"] = bool(
        inventory_valid
        and (result["provenance"] is None or result["provenance"]["consistent"])
    )
    result["core_ready"] = False
    result["core_blockers"] = ["financial_as_of_unknown", "financial_units_unknown"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_f10_inventory(args.csv, provenance_path=args.provenance)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    print(content, end="")
    return 0 if result["accepted_for_identity"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
