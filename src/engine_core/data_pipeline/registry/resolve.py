"""Resolve an artifact's concrete adapter from registered source identities."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from .spec import DatasetSpec


class AdapterResolution(NamedTuple):
    adapter: str | None
    resolved_by: str


def _native_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value in {"ABSENT", "UNKNOWN"}:
        return None
    return None if value.startswith("@ctx:") else value


def _row_shape_adapter(spec: DatasetSpec,
                       sample_rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    rows = [row for row in (sample_rows or ())[:200] if isinstance(row, Mapping)]
    if not rows:
        return None
    adapters = {adapter for field in spec.fields for adapter in field.source_units}
    keys_by_adapter = {
        adapter: {key for field in spec.fields
                  if (key := _native_key(field.source_keys.get(adapter))) is not None}
        for adapter in adapters
    }
    candidates = []
    for adapter, native_keys in keys_by_adapter.items():
        required = set()
        for field in spec.fields:
            key = _native_key(field.source_keys.get(adapter))
            code = field.source_units.get(adapter, "UNKNOWN")
            if key and code not in {"ABSENT", "UNKNOWN"} and (
                not field.nullable or field.name in spec.required_fields
            ):
                required.add(key)
        if not required or any(
            sum(key in row for row in rows) / len(rows) < 0.99 for key in required
        ):
            continue
        exclusive = set().union(*(
            keys for other, keys in keys_by_adapter.items() if other != adapter
        )) if len(keys_by_adapter) > 1 else set()
        unique_keys = native_keys - exclusive
        if any(key in row for key in unique_keys for row in rows):
            candidates.append(adapter)
    return candidates[0] if len(candidates) == 1 else None


def resolve_adapter(spec: DatasetSpec, manifest: Mapping[str, Any] | None,
                    source_label: str | None,
                    sample_rows: Sequence[Mapping[str, Any]] | None = None) -> AdapterResolution:
    """Resolve by explicit metadata first, then source label and captured row shape."""
    registered = {adapter for field in spec.fields for adapter in field.source_units}
    metadata = manifest if isinstance(manifest, Mapping) else {}

    adapter = metadata.get("adapter")
    if isinstance(adapter, str) and adapter in registered:
        return AdapterResolution(adapter, "adapter")

    attempts = metadata.get("provider_attempts")
    if isinstance(attempts, (list, tuple)):
        accepted = [item for item in attempts
                    if isinstance(item, Mapping) and item.get("result") == "ACCEPTED"]
        if accepted:
            provider = accepted[-1].get("provider")
            if isinstance(provider, str) and provider in registered:
                return AdapterResolution(provider, "provider_attempts")

    if isinstance(source_label, str) and source_label in registered:
        return AdapterResolution(source_label, "source")
    alias = spec.source_aliases.get(source_label) if isinstance(source_label, str) else None
    if isinstance(alias, str) and alias in registered:
        return AdapterResolution(alias, "alias")
    inferred = _row_shape_adapter(spec, sample_rows)
    if inferred is not None:
        return AdapterResolution(inferred, "row_shape")
    return AdapterResolution(None, "unresolved")
