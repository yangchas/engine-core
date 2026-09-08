import pytest

from engine_core import EvaluationNode, EvaluationPlan


def _plan():
    return EvaluationPlan(
        plan_id="auction-shadow",
        version="v1",
        nodes=(
            EvaluationNode(
                node_id="AUCTION_0920_FACTS",
                trigger_id="AUCTION_0920",
                data_requirements=("previous_day_stats",),
                fact_functions=("build_segment_frame",),
                strategies=("auction_fact_shadow",),
            ),
            EvaluationNode(
                node_id="AUCTION_0924_COMPARE",
                trigger_id="AUCTION_0924",
                data_requirements=("previous_day_stats",),
                fact_functions=("build_segment_frame", "compare_adjacent_segments"),
                strategies=("auction_fact_shadow",),
            ),
        ),
    )


def test_evaluation_plan_preserves_declared_business_order():
    plan = _plan()
    node = plan.node_for_trigger("AUCTION_0924")
    assert node.data_requirements == ("previous_day_stats",)
    assert node.fact_functions == (
        "build_segment_frame",
        "compare_adjacent_segments",
    )
    assert node.strategies == ("auction_fact_shadow",)


def test_evaluation_plan_is_immutable_and_hash_stable():
    left = _plan()
    right = _plan()
    assert left.content_hash == right.content_hash
    assert left.nodes_by_trigger()["AUCTION_0920"].content_hash == left.nodes[0].content_hash
    with pytest.raises(TypeError):
        left.nodes_by_trigger()["X"] = left.nodes[0]


def test_evaluation_node_order_is_semantic_for_data_and_facts():
    left = EvaluationNode(
        "N",
        "T",
        data_requirements=("A", "B"),
        fact_functions=("F1", "F2"),
    )
    right = EvaluationNode(
        "N",
        "T",
        data_requirements=("B", "A"),
        fact_functions=("F1", "F2"),
    )
    assert left.content_hash != right.content_hash


def test_evaluation_contract_rejects_duplicate_or_unknown_identity():
    with pytest.raises(TypeError, match="iterable of names"):
        EvaluationNode("N", "T", data_requirements="previous_day_stats")
    with pytest.raises(ValueError, match="duplicates"):
        EvaluationNode("N", "T", data_requirements=("A", "A"))
    node = EvaluationNode("N", "T")
    with pytest.raises(ValueError, match="node_id values"):
        EvaluationPlan("P", "v1", (node, EvaluationNode("N", "T2")))
    with pytest.raises(ValueError, match="trigger_id values"):
        EvaluationPlan("P", "v1", (node, EvaluationNode("N2", "T")))
    with pytest.raises(KeyError, match="unknown evaluation trigger"):
        EvaluationPlan("P", "v1", (node,)).node_for_trigger("UNKNOWN")


def test_evaluation_plan_contains_no_execution_or_workflow_behavior():
    plan = _plan()
    forbidden = {
        "execute",
        "run",
        "retry",
        "next_node",
        "dependencies",
        "condition",
    }
    assert forbidden.isdisjoint(dir(plan))
    assert forbidden.isdisjoint(dir(plan.nodes[0]))
