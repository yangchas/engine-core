from datetime import datetime, timezone

import pytest

from engine_core.contracts import (
    canonical_hash,
    canonical_json,
    deep_freeze,
    evidence_hash,
    semantic_hash,
    trunc_div,
)


def test_canonical_hash_is_order_independent_for_mapping_keys():
    left = {"b": 2, "a": 1}
    right = {"a": 1, "b": 2}
    assert canonical_hash(left) == canonical_hash(right)
    assert canonical_json(left) == '{"a":1,"b":2}'


def test_canonical_contract_rejects_ambiguous_values():
    with pytest.raises(ValueError):
        canonical_json(float("nan"))
    with pytest.raises(ValueError):
        canonical_json(datetime(2026, 9, 4, 1, 0))
    with pytest.raises(TypeError):
        canonical_json({1: "non-string-key"})
    with pytest.raises(TypeError):
        canonical_json({"values": {1, 2}})


def test_deep_freeze_does_not_normalize_or_reorder_business_values():
    source = {"values": [3, 1], "nested": {"b": 2}}
    frozen = deep_freeze(source)
    source["values"].append(0)
    source["nested"]["a"] = 1
    assert tuple(frozen["values"]) == (3, 1)
    assert dict(frozen["nested"]) == {"b": 2}
    with pytest.raises(TypeError):
        frozen["nested"]["c"] = 3


def test_deep_freeze_rejects_unordered_sets_instead_of_inventing_order():
    with pytest.raises(TypeError):
        deep_freeze({"symbols": {"000001", "000002"}})


def test_hash_kinds_are_distinct_and_versioned():
    value = {"a": 1}
    assert semantic_hash(value) != evidence_hash(value)
    assert semantic_hash(value, schema_version=1) != semantic_hash(value, schema_version=2)


def test_trunc_div_matches_c_toward_zero_for_signed_values():
    assert trunc_div(100, 3) == 33
    assert trunc_div(-100, 3) == -33
    assert trunc_div(100, -3) == -33
    assert trunc_div(-100, -3) == 33
    with pytest.raises(ZeroDivisionError):
        trunc_div(1, 0)
