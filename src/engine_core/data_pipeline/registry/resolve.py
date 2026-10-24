"""Resolve an artifact's concrete adapter from registered source identities."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .spec import DatasetSpec


def resolve_adapter(spec: DatasetSpec, manifest: Mapping[str, Any] | None,
                    source_label: str | None) -> str | None:
    """Choose the first registered adapter supported by manifest evidence."""
    registered = {adapter for field in spec.fields for adapter in field.source_units}
    metadata = manifest if isinstance(manifest, Mapping) else {}

    adapter = metadata.get("adapter")
    if isinstance(adapter, str) and adapter in registered:
        return adapter

    attempts = metadata.get("provider_attempts")
    if isinstance(attempts, (list, tuple)):
        accepted = [item for item in attempts
                    if isinstance(item, Mapping) and item.get("result") == "ACCEPTED"]
        if accepted:
            provider = accepted[-1].get("provider")
            if isinstance(provider, str) and provider in registered:
                return provider

    if isinstance(source_label, str) and source_label in registered:
        return source_label
    return None
