"""Observational quality summaries for registered dataset commits."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from .canonical import canonicalize_row
from .checks import evaluate_checks
from .resolve import AdapterResolution, resolve_adapter
from .spec import DatasetSpec


def calculate_gate_status(*, adapter: str | None, key_invalid: int | None,
                          flagged_rows: int, rows: int, max_bad_ratio: float,
                          flags_by_field: Mapping[str, int],
                          unmapped_keys: Mapping[str, int],
                          checks: Mapping[str, Mapping[str, Any]],
                          coverage_reasons: Sequence[str] = ()) -> str:
    """Classify a gate summary without side effects."""
    if adapter is None or key_invalid is None or key_invalid > 0 or coverage_reasons:
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
                        source: str, explicit: str | None,
                        sample_rows: Sequence[Mapping[str, Any]] | None) -> AdapterResolution:
    if explicit is not None:
        return resolve_adapter(spec, {"adapter": explicit}, None)
    return resolve_adapter(spec, manifest, source, sample_rows=sample_rows)


def _check_summary(results: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: {key: result[key] for key in ("status", "passed", "evaluated", "skipped")}
            for name, result in results.items()}


def summarize_gate(spec: DatasetSpec, rows: Sequence[Mapping[str, Any]], *,
                   trade_date: str, manifest: Mapping[str, Any] | None,
                   source: str, adapter: str | None = None,
                   context: Mapping[str, Any] | None = None,
                   sample_rows: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Canonicalize every row for analysis while leaving stored rows untouched."""
    sample = rows[:200] if sample_rows is None else sample_rows[:200]
    resolution = _adapter_resolution(spec, manifest, source, adapter, sample)
    metadata = manifest if isinstance(manifest, Mapping) else {}
    if resolution.adapter is None:
        checks = {item.name: {"status": "INERT", "passed": 0, "evaluated": 0,
                              "skipped": len(rows)} for item in spec.checks}
        return {
            "schema_version": spec.schema_version,
            "adapter": None,
            "adapter_resolved_by": "unresolved",
            "mode": spec.gate_mode,
            "rows": len(rows),
            "flagged_rows": None,
            "flags_by_field": {},
            "unmapped_keys": {},
            "key_invalid": None,
            "checks": checks,
            "gate_status": "FAIL",
            "reasons": ["adapter_unresolved"],
        }

    row_context = dict(context) if isinstance(context, Mapping) else {}
    row_context["trade_date"] = trade_date
    canonical_rows = [canonicalize_row(spec, resolution.adapter, row, row_context) for row in rows]
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
    checks = _check_summary(evaluate_checks(spec, canonical_rows, resolution.adapter))
    coverage_reasons: list[str] = []
    if spec.min_coverage is not None:
        quality = metadata.get("quality")
        quality = quality if isinstance(quality, Mapping) else {}
        coverage = quality.get("coverage")
        valid_coverage = (
            not isinstance(coverage, bool) and isinstance(coverage, (int, float))
            and 0 <= coverage <= 1 and isfinite(coverage)
        )
        if not valid_coverage:
            coverage_reasons.append("coverage_missing")
        if quality.get("status") == "PARTIAL" or (valid_coverage and coverage < spec.min_coverage):
            coverage_reasons.append("coverage_low")
    reasons = list(coverage_reasons)
    if key_invalid:
        reasons.append("key_invalid")
    if rows and flagged_rows / len(rows) > spec.max_bad_ratio:
        reasons.append("bad_ratio")
    reasons.extend(f"below_min:{name}" for name, item in checks.items()
                   if item.get("status") == "BELOW_MIN")
    gate_status = calculate_gate_status(
        adapter=resolution.adapter, key_invalid=key_invalid, flagged_rows=flagged_rows,
        rows=len(rows), max_bad_ratio=spec.max_bad_ratio,
        flags_by_field=flags_by_field, unmapped_keys=unmapped_keys, checks=checks,
        coverage_reasons=coverage_reasons,
    )
    if gate_status == "WARN":
        if flags_by_field:
            reasons.append("field_flags")
        if unmapped_keys:
            reasons.append("unmapped_keys")
        if any(item.get("status") == "INERT" for item in checks.values()):
            reasons.append("check_inert")
    return {
        "schema_version": spec.schema_version,
        "adapter": resolution.adapter,
        "adapter_resolved_by": resolution.resolved_by,
        "mode": spec.gate_mode,
        "rows": len(rows),
        "flagged_rows": flagged_rows,
        "flags_by_field": dict(sorted(flags_by_field.items())),
        "unmapped_keys": dict(sorted(unmapped_keys.items())),
        "key_invalid": key_invalid,
        "checks": checks,
        "gate_status": gate_status,
        "reasons": reasons,
    }
