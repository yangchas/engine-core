"""Whitelist-based evaluation of declarative cross-field checks."""
from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from .spec import DatasetSpec, _check_field_names, _validate_check_expression


_FUNCTIONS = {"abs": abs, "round": round, "min": min, "max": max}


def _has_null_guard(expression: str, missing: set[str]) -> bool:
    tree = ast.parse(expression, mode="eval").body
    if not isinstance(tree, ast.BoolOp) or not isinstance(tree.op, ast.Or):
        return False
    return any(
        isinstance(branch, ast.Compare)
        and len(branch.ops) == 1
        and isinstance(branch.ops[0], ast.Is)
        and isinstance(branch.left, ast.Name)
        and branch.left.id in missing
        and len(branch.comparators) == 1
        and isinstance(branch.comparators[0], ast.Constant)
        and branch.comparators[0].value is None
        for branch in tree.values
    )


def evaluate_checks(spec: DatasetSpec, canonical_rows: Sequence[Any],
                    adapter: str | None = None) -> dict[str, dict[str, Any]]:
    """Return per-check pass/evaluation/skip counts for canonical rows or value mappings."""
    result: dict[str, dict[str, Any]] = {}
    for check in spec.checks:
        field_names = {field.name for field in spec.fields}
        _validate_check_expression(check.expr, field_names)
        referenced = _check_field_names(check.expr)
        required_fields = {field.name: field for field in spec.fields}
        inapplicable = adapter is not None and (
            (check.adapters and adapter not in check.adapters)
            or any(required_fields[name].source_units.get(adapter) == "ABSENT"
                   for name in check.requires)
        )
        if inapplicable:
            result[check.name] = {
                "passed": 0, "evaluated": 0, "skipped": len(canonical_rows),
                "pass_ratio": None, "min_pass_ratio": check.min_pass_ratio,
                "status": "INAPPLICABLE",
            }
            continue
        code = compile(ast.parse(check.expr, mode="eval"), "<registry-check>", "eval")
        passed = evaluated = skipped = 0
        for row in canonical_rows:
            if isinstance(row, Mapping):
                values = row
            else:
                candidate = getattr(row, "values", None)
                values = candidate if isinstance(candidate, Mapping) else {}
            missing = {name for name in referenced if values.get(name) is None}
            if missing and not _has_null_guard(check.expr, missing):
                skipped += 1
                continue
            try:
                outcome = eval(code, {"__builtins__": {}, **_FUNCTIONS}, dict(values))
            except (ArithmeticError, TypeError, ValueError):
                skipped += 1
                continue
            evaluated += 1
            passed += int(bool(outcome))
        pass_ratio = passed / evaluated if evaluated else None
        status = "INERT" if evaluated == 0 else (
            "OK" if pass_ratio >= check.min_pass_ratio else "BELOW_MIN"
        )
        result[check.name] = {
            "passed": passed, "evaluated": evaluated, "skipped": skipped,
            "pass_ratio": pass_ratio, "min_pass_ratio": check.min_pass_ratio,
            "status": status,
        }
    return result
