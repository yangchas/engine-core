"""Explicit source selection; no fallback or unit coercion is implicit."""
from __future__ import annotations


ALLOWED_SOURCES = frozenset({"legacy_shared_q2", "legacy_td"})


def choose_source(sources, selected=None):
    if selected is None:
        if len(sources) != 1:
            raise ValueError("explicit source required when multiple sources are configured")
        selected = next(iter(sources))
    if selected not in ALLOWED_SOURCES or selected not in sources:
        raise ValueError("source must be explicitly configured and allow-listed")
    return sources[selected]

