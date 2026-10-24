"""Pure row normalization into a dataset's declared canonical fields."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .convert import ConversionError, to_canonical
from .spec import DatasetSpec, FieldSpec


@dataclass(frozen=True)
class CanonicalRow:
    values: dict[str, Any]
    flags: dict[str, str]
    unmapped_keys: list[str]


def _source_key(field: FieldSpec, adapter: str, row: Mapping[str, Any]) -> str | None:
    declared = field.source_keys.get(adapter)
    if not isinstance(declared, str) or not declared or declared == "UNKNOWN":
        return None
    if declared in row:
        return declared
    marker = "[YYYYMMDD]"
    if marker in declared:
        pattern = re.escape(declared).replace(r"\[YYYYMMDD\]", r"\[\d{8}\]")
        return next((key for key in sorted(key for key in row if isinstance(key, str))
                     if re.fullmatch(pattern, key)), None)
    return None


def _has_type(value: Any, expected: type) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _null_error(field: FieldSpec, required: set[str]) -> str | None:
    if field.name in required:
        return "MISSING_REQUIRED"
    if not field.nullable:
        return "NULL_NOT_ALLOWED"
    return None


def canonicalize_row(spec: DatasetSpec, adapter: str | None,
                     row: Mapping[str, Any]) -> CanonicalRow:
    """Normalize a row without discarding it or raising on row-level errors."""
    raw = row if isinstance(row, Mapping) else {}
    values: dict[str, Any] = {}
    flags: dict[str, str] = {}
    mapped: set[str] = set()
    registered = {name for field in spec.fields for name in field.source_units}
    if adapter is None or adapter not in registered:
        return CanonicalRow(
            values={field.name: None for field in spec.fields},
            flags={field.name: "ADAPTER_UNKNOWN" for field in spec.fields},
            unmapped_keys=sorted(str(key) for key in raw),
        )

    required = set(spec.required_fields)
    for field in spec.fields:
        source_code = field.source_units.get(adapter, "UNKNOWN")
        if source_code == "ABSENT":
            values[field.name] = None
            error = _null_error(field, required)
            if error:
                flags[field.name] = error
            continue

        native_key = _source_key(field, adapter, raw)
        if native_key is None:
            values[field.name] = None
            if source_code == "UNKNOWN":
                flags[field.name] = "UNPARSEABLE"
            else:
                error = _null_error(field, required)
                if error:
                    flags[field.name] = error
            continue

        mapped.add(native_key)
        converted = to_canonical(raw[native_key], source_code, field.canonical_unit)
        if isinstance(converted, ConversionError):
            values[field.name] = None
            flags[field.name] = "UNPARSEABLE"
            continue
        if converted is None:
            values[field.name] = None
            error = _null_error(field, required)
            if error:
                flags[field.name] = error
            continue
        if not _has_type(converted, field.type):
            values[field.name] = None
            flags[field.name] = "BAD_TYPE"
            continue
        values[field.name] = converted

    return CanonicalRow(
        values=values,
        flags=flags,
        unmapped_keys=sorted(str(key) for key in raw if key not in mapped),
    )
