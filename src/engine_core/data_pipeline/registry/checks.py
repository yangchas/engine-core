"""Whitelist-based evaluation of declarative cross-field checks."""
from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from .spec import DatasetSpec, _check_field_names, _validate_check_expression


_FUNCTIONS = {"abs": abs, "round": round, "min": min, "max": max}


def evaluate_checks(spec: DatasetSpec, canonical_rows: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Return per-check pass/evaluation/skip counts for canonical rows or value mappings."""
    result: dict[str, dict[str, int]] = {}
    for check in spec.checks:
        field_names = {field.name for field in spec.fields}
        _validate_check_expression(check.expr, field_names)
        referenced = _check_field_names(check.expr)
        code = compile(ast.parse(check.expr, mode="eval"), "<registry-check>", "eval")
        passed = evaluated = skipped = 0
        for row in canonical_rows:
            if isinstance(row, Mapping):
                values = row
            else:
                candidate = getattr(row, "values", None)
                values = candidate if isinstance(candidate, Mapping) else {}
            if any(values.get(name) is None for name in referenced):
                skipped += 1
                continue
            try:
                outcome = eval(code, {"__builtins__": {}, **_FUNCTIONS}, dict(values))
            except (ArithmeticError, TypeError, ValueError):
                skipped += 1
                continue
            evaluated += 1
            passed += int(bool(outcome))
        result[check.name] = {"passed": passed, "evaluated": evaluated, "skipped": skipped}
    return result
