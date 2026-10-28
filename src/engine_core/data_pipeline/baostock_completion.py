"""Completion policy for Baostock partitions with known source gaps."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence, Set
from typing import Any


CATEGORIES = frozenset({"SOURCE_UNAVAILABLE", "SOURCE_EMPTY", "UNEXPECTED"})


def _symbol(value: Any) -> str:
    raw = str(value).strip().rsplit(".", 1)[-1]
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    if raw.isdigit() and len(raw) <= 6:
        return raw.zfill(6)
    raise ValueError("baostock_completion_invalid_symbol")


def _symbols(values: Sequence[Any] | Set[Any]) -> set[str]:
    return {_symbol(value) for value in values}


def classify_missing_symbols(expected_symbols: Sequence[Any] | Set[Any],
                             bar_symbols: Sequence[Any] | Set[Any],
                             source_symbols: Sequence[Any] | Set[Any] | None
                             ) -> dict[str, str]:
    """Classify every missing expected symbol using Baostock's same-day stock list."""
    expected = _symbols(expected_symbols)
    present = _symbols(bar_symbols)
    if not present <= expected:
        present &= expected
    source = _symbols(source_symbols) if source_symbols is not None else None
    missing = expected - present
    if source is None:
        return {symbol: "UNEXPECTED" for symbol in sorted(missing)}
    return {symbol: ("SOURCE_EMPTY" if symbol in source else "SOURCE_UNAVAILABLE")
            for symbol in sorted(missing)}


def evaluate_baostock_completion(expected_symbols: Sequence[Any] | Set[Any],
                                 bar_symbols: Sequence[Any] | Set[Any],
                                 known_missing: Mapping[str, str], *,
                                 coverage_threshold: float,
                                 max_source_empty: int) -> dict[str, Any]:
    """Validate the category ledger and compute quality, alert, and retry semantics."""
    expected = _symbols(expected_symbols)
    present = _symbols(bar_symbols) & expected
    normalized = {_symbol(key): value for key, value in known_missing.items()}
    missing = expected - present
    if set(normalized) != missing:
        raise ValueError("known_missing_incomplete")
    if any(value not in CATEGORIES for value in normalized.values()):
        raise ValueError("known_missing_category_invalid")
    if isinstance(coverage_threshold, bool) or not isinstance(coverage_threshold, (int, float)) or \
            not math.isfinite(coverage_threshold) or not 0 < coverage_threshold <= 1:
        raise ValueError("baostock_coverage_threshold_invalid")
    if type(max_source_empty) is not int or max_source_empty < 0:
        raise ValueError("baostock_source_empty_max_invalid")

    unavailable = sum(category == "SOURCE_UNAVAILABLE" for category in normalized.values())
    empty = sum(category == "SOURCE_EMPTY" for category in normalized.values())
    unexpected = sum(category == "UNEXPECTED" for category in normalized.values())
    effective_expected = len(expected) - unavailable
    coverage = (len(present) / effective_expected) if effective_expected else 1.0
    alert_required = (unexpected > 0 or empty > max_source_empty or
                      coverage < float(coverage_threshold))
    formal = not alert_required
    quality = {
        "known_missing": dict(sorted(normalized.items())),
        "expected_count": len(expected),
        "effective_expected_count": effective_expected,
        "covered_count": len(present),
        "coverage": coverage,
        "missing": len(missing),
        "source_unavailable_count": unavailable,
        "source_empty_count": empty,
        "unexpected_count": unexpected,
        "coverage_threshold": float(coverage_threshold),
        "source_empty_max": max_source_empty,
        "partial": not formal,
        "status": "READY" if formal else "PARTIAL",
        "alert_required": alert_required,
    }
    return {"quality": quality,
            "business_status": "FORMAL" if formal else "PARTIAL",
            "alert_required": alert_required,
            "retryable": not formal,
            "error_code": None if formal else "TIER_PARTIAL",
            "exit_code": 0 if formal else 30}
