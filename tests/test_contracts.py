from datetime import datetime, timezone

import pytest

from engine_core.contracts import canonical_hash, canonical_json


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
