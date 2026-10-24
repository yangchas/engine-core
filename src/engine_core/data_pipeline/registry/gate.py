"""Observational quality summaries for registered dataset commits."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonicalize_row
from .checks import evaluate_checks
from .resolve import resolve_adapter
from .spec import DatasetSpec


def calculate_gate_status(*, adapter: str | None, key_invalid: int,
                          flagged_rows: int, rows: int, max_bad_ratio: float,
                          flags_by_field: Mapping[str, int],
                          unmapped_keys: Mapping[str, int],
                          checks: Mapping[str, Mapping[str, Any]]) -> str:
    """Classify a gate summary without side effects."""
    if adapter is None or key_invalid > 0:
        return "FAIL"
    if rows and flagged_rows / rows > max_bad_ratio:
        return "FAIL"
    if any(item.get("status") == "BELOW_MIN" for item in checks.values()):
        return "FAIL"
    if flags_by_field or unmapped_keys or any(
        item.get("status") == "INERT" for item in checks.values()
    ):
        return "WARN"
    return "OK"


def _adapter_resolution(spec: DatasetSpec, manifest: Mapping[str, Any] | None,
                        source: str, explicit: str | None) -> tuple[str | None, str]:
    metadata = manifest if isinstance(manifest, Mapping) else {}
    if explicit is not None:
        resolved = resolve_adapter(spec, {"adapter": explicit}, None)
        return resolved, "explicit" if resolved is not None else "unresolved"

    resolved = resolve_adapter(spec, metadata, source)
    registered = {adapter for item in spec.fields for adapter in item.source_units}
    if metadata.get("adapter") in registered and metadata.get("adapter") == resolved:
        return resolved, "manifest"
    attempts = metadata.get("provider_attempts")
    if isinstance(attempts, (list, tuple)):
        accepted = [item for item in attempts
                    if isinstance(item, Mapping) and item.get("result") == "ACCEPTED"]
        if accepted and accepted[-1].get("provider") == resolved and resolved is not None:
            return resolved, "provider_attempts"
    if source in registered and source == resolved:
        return resolved, "source"
    return resolved, "unresolved"


def summarize_gate(spec: DatasetSpec, rows: Sequence[Mapping[str, Any]], *,
                   trade_date: str, manifest: Mapping[str, Any] | None,
                   source: str, adapter: str | None = None,
                   context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Canonicalize every row for analysis while leaving stored rows untouched."""
    resolved_adapter, resolved_by = _adapter_resolution(spec, manifest, source, adapter)
    row_context = dict(context) if isinstance(context, Mapping) else {}
    row_context["trade_date"] = trade_date
    canonical_rows = [canonicalize_row(spec, resolved_adapter, row, row_context) for row in rows]

    flags_by_field: Counter[str] = Counter()
    unmapped_keys: Counter[str] = Counter()
    key_values: list[tuple[Any, ...] | None] = []
    for raw, canonical in zip(rows, canonical_rows):
        flags_by_field.update(canonical.flags.keys())
        unmapped_keys.update(canonical.unmapped_keys)
        values = []
        invalid = False
        for name in spec.key_fields:
            if name in canonical.values:
                value = canonical.values[name]
            elif isinstance(raw, Mapping) and name in raw:
                value = raw[name]
            else:
                value = row_context.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                invalid = True
                break
            values.append(value)
        if invalid:
            key_values.append(None)
            continue
        try:
            hash(tuple(values))
        except TypeError:
            key_values.append(None)
        else:
            key_values.append(tuple(values))

    key_counts = Counter(key for key in key_values if key is not None)
    key_invalid = (sum(key is None or key_counts[key] > 1 for key in key_values)
                   if spec.key_fields else 0)
    flagged_rows = sum(bool(item.flags) for item in canonical_rows)
    check_results = evaluate_checks(spec, canonical_rows, resolved_adapter)
    checks = {
        name: {key: result[key] for key in ("status", "passed", "evaluated", "skipped")}
        for name, result in check_results.items()
    }
    gate_status = calculate_gate_status(
        adapter=resolved_adapter, key_invalid=key_invalid, flagged_rows=flagged_rows,
        rows=len(rows), max_bad_ratio=spec.max_bad_ratio,
        flags_by_field=flags_by_field, unmapped_keys=unmapped_keys, checks=checks,
    )
    return {
        "schema_version": spec.schema_version,
        "adapter": resolved_adapter,
        "adapter_resolved_by": resolved_by,
        "mode": spec.gate_mode,
        "rows": len(rows),
        "flagged_rows": flagged_rows,
        "flags_by_field": dict(sorted(flags_by_field.items())),
        "unmapped_keys": dict(sorted(unmapped_keys.items())),
        "key_invalid": key_invalid,
        "checks": checks,
        "gate_status": gate_status,
    }
