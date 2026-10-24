"""Declarative dataset, field, and cross-field check specifications."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


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


@dataclass(frozen=True)
class CheckSpec:
    name: str
    expr: str
    min_pass_ratio: float


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
