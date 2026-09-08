"""Plain immutable descriptions of what one trigger evaluates.

EvaluationPlan is deliberately not an executor or workflow language.  It has
no conditions, dependency graph, retry policy, concurrency semantics, or
strategy chaining.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .contracts import semantic_hash


EVALUATION_PLAN_CONTRACT_VERSION = "EvaluationPlanV1"


def _ordered_unique_names(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of names, not text")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of names") from exc
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"{field_name} must contain non-empty strings")
    canonical = tuple(item.strip() for item in result)
    if len(canonical) != len(set(canonical)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return canonical


@dataclass(frozen=True)
class EvaluationNode:
    """What to prepare and run for one already-triggered evaluation."""

    node_id: str
    trigger_id: str
    data_requirements: tuple[str, ...] = ()
    fact_functions: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("node_id is required")
        if not isinstance(self.trigger_id, str) or not self.trigger_id.strip():
            raise ValueError("trigger_id is required")
        object.__setattr__(self, "node_id", self.node_id.strip())
        object.__setattr__(self, "trigger_id", self.trigger_id.strip())
        object.__setattr__(
            self,
            "data_requirements",
            _ordered_unique_names(
                self.data_requirements,
                field_name="data_requirements",
            ),
        )
        object.__setattr__(
            self,
            "fact_functions",
            _ordered_unique_names(self.fact_functions, field_name="fact_functions"),
        )
        object.__setattr__(
            self,
            "strategies",
            _ordered_unique_names(self.strategies, field_name="strategies"),
        )
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": EVALUATION_PLAN_CONTRACT_VERSION,
                    "node_id": self.node_id,
                    "trigger_id": self.trigger_id,
                    "data_requirements": self.data_requirements,
                    "fact_functions": self.fact_functions,
                    "strategies": self.strategies,
                }
            ),
        )


@dataclass(frozen=True)
class EvaluationPlan:
    """Ordered set of evaluation nodes with exact trigger lookup."""

    plan_id: str
    version: str
    nodes: tuple[EvaluationNode, ...]
    content_hash: str = field(init=False)
    _by_trigger: Mapping[str, EvaluationNode] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("plan_id is required")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("evaluation plan version is required")
        nodes = tuple(self.nodes)
        if not nodes:
            raise ValueError("evaluation plan requires at least one node")
        if any(not isinstance(node, EvaluationNode) for node in nodes):
            raise TypeError("nodes must contain EvaluationNode values")
        node_ids = tuple(node.node_id for node in nodes)
        trigger_ids = tuple(node.trigger_id for node in nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node_id values must be unique")
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("trigger_id values must be unique")
        object.__setattr__(self, "plan_id", self.plan_id.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(
            self,
            "_by_trigger",
            MappingProxyType({node.trigger_id: node for node in nodes}),
        )
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": EVALUATION_PLAN_CONTRACT_VERSION,
                    "plan_id": self.plan_id,
                    "version": self.version,
                    "nodes": tuple(
                        {
                            "node_id": node.node_id,
                            "trigger_id": node.trigger_id,
                            "content_hash": node.content_hash,
                        }
                        for node in nodes
                    ),
                }
            ),
        )

    def node_for_trigger(self, trigger_id: str) -> EvaluationNode:
        """Return the one node registered for an exact trigger identity."""

        if not isinstance(trigger_id, str) or not trigger_id:
            raise ValueError("trigger_id is required")
        try:
            return self._by_trigger[trigger_id]
        except KeyError as exc:
            raise KeyError("unknown evaluation trigger: " + trigger_id) from exc

    def nodes_by_trigger(self) -> Mapping[str, EvaluationNode]:
        return self._by_trigger
