"""Declarative dataset, field, and cross-field check specifications."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Mapping


_CHECK_FUNCTIONS = {"abs", "round", "min", "max"}
_CHECK_NODES = {
    ast.Expression, ast.Name, ast.Load, ast.Constant, ast.Call, ast.Tuple,
    ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BinOp, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq,
    ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
}


def _validate_check_expression(expression: str, field_names: set[str] | None = None) -> None:
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, TypeError) as exc:
        raise ValueError(f"invalid check expression: {expression!r}") from exc
    for node in ast.walk(tree):
        if type(node) not in _CHECK_NODES:
            raise ValueError(f"unsupported check expression node: {type(node).__name__}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError("check constants must be numbers")
        if isinstance(node, ast.Name) and node.id not in _CHECK_FUNCTIONS:
            if field_names is not None and node.id not in field_names:
                raise ValueError(f"unknown check field: {node.id}")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name)
            or node.func.id not in _CHECK_FUNCTIONS
            or node.keywords
        ):
            raise ValueError("check calls must use abs, round, min, or max without keywords")


def _check_field_names(expression: str) -> set[str]:
    tree = ast.parse(expression, mode="eval")
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in _CHECK_FUNCTIONS}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: type
    nullable: bool
    canonical_unit: str
    source_units: Mapping[str, str]
    serves: tuple[str, ...] = ()
    semantics: str = "UNVERIFIED"
    description: str = ""
    source_keys: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckSpec:
    name: str
    expr: str
    min_pass_ratio: float

    def __post_init__(self) -> None:
        _validate_check_expression(self.expr)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source: str
    schedule: str
    primary_backend: str = "artifact"
    hot_view: bool = True
    redis_ttl_seconds: int = 604800
    max_redis_payload_bytes: int = 65536
    retention_policy: str = "immutable_versions"
    dependencies: tuple[str, ...] = ()
    point_in_time: bool = True
    source_dependency: str = "independent_provider"
    fetch_budget_scope: str = "default"
    empty_is_valid: bool = False
    key_fields: tuple[str, ...] = ("symbol",)
    required_fields: tuple[str, ...] = ()
    schema_version: int = 1
    fields: tuple[FieldSpec, ...] = ()
    checks: tuple[CheckSpec, ...] = ()

    def __post_init__(self) -> None:
        field_names = {item.name for item in self.fields}
        for check in self.checks:
            _validate_check_expression(check.expr, field_names)
