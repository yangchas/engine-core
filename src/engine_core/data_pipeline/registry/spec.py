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
    ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is,
}


def _validate_check_expression(expression: str, field_names: set[str] | None = None) -> None:
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, TypeError) as exc:
        raise ValueError(f"invalid check expression: {expression!r}") from exc
    for node in ast.walk(tree):
        if type(node) not in _CHECK_NODES:
            raise ValueError(f"unsupported check expression node: {type(node).__name__}")
        if isinstance(node, ast.Constant) and node.value is not None and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError("check constants must be numbers or None")
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
    raw_only: bool = False


@dataclass(frozen=True)
class CheckSpec:
    name: str
    expr: str
    min_pass_ratio: float
    requires: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()

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
    gate_mode: str = "observe"
    max_bad_ratio: float = 0.02
    source_aliases: Mapping[str, str] = field(default_factory=dict)
    min_coverage: float | None = None
    source_preference: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.gate_mode not in {"off", "observe", "enforce"}:
            raise ValueError(f"invalid gate mode: {self.gate_mode!r}")
        if isinstance(self.max_bad_ratio, bool) or not isinstance(self.max_bad_ratio, (int, float)) or not (
            0 <= self.max_bad_ratio <= 1
        ):
            raise ValueError("max_bad_ratio must be between 0 and 1")
        if self.min_coverage is not None and (
            isinstance(self.min_coverage, bool) or not isinstance(self.min_coverage, (int, float))
            or not 0 <= self.min_coverage <= 1
        ):
            raise ValueError("min_coverage must be between 0 and 1")
        field_names = {item.name for item in self.fields}
        adapters = {adapter for item in self.fields for adapter in item.source_units}
        if set(self.source_aliases.values()) - adapters:
            raise ValueError("source aliases must target registered adapters")
        raw_only_names = {item.name for item in self.fields if item.raw_only}
        for check in self.checks:
            _validate_check_expression(check.expr, field_names)
            checked_raw_only = _check_field_names(check.expr) & raw_only_names
            if checked_raw_only:
                raise ValueError(f"raw-only fields cannot be checked: {sorted(checked_raw_only)}")
            unknown_required = set(check.requires) - field_names
            if unknown_required:
                raise ValueError(f"unknown required check fields: {sorted(unknown_required)}")
            unknown_adapters = set(check.adapters) - adapters
            if unknown_adapters:
                raise ValueError(f"unknown check adapters: {sorted(unknown_adapters)}")
