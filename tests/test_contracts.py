from datetime import datetime, timezone

import pytest

from engine_core.contracts import (
    canonical_hash,
    canonical_json,
    deep_freeze,
    evidence_hash,
    EngineSignal,
    SignalKind,
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


def test_deep_freeze_fails_closed_for_unknown_mutable_objects():
    class MutablePayload:
        pass

    with pytest.raises(TypeError, match="unsupported value"):
        deep_freeze(MutablePayload())


def test_hash_kinds_are_distinct_and_versioned():
    value = {"a": 1}
    assert semantic_hash(value) != evidence_hash(value)
    assert semantic_hash(value, schema_version=1) != semantic_hash(value, schema_version=2)


def test_signal_priority_and_sort_key_are_explicit_contracts():
    assert SignalKind.MARKET_UPDATE.priority < SignalKind.DATA_READY.priority
    assert SignalKind.DATA_READY.priority < SignalKind.TIMER.priority
    signal = EngineSignal("s", 100, 7, SignalKind.TIMER, {})
    assert signal.sort_key == (100, SignalKind.TIMER.priority, 7, "s")


def test_trunc_div_matches_c_toward_zero_for_signed_values():
    assert trunc_div(100, 3) == 33
    assert trunc_div(-100, 3) == -33
    assert trunc_div(100, -3) == -33
    assert trunc_div(-100, -3) == 33
    with pytest.raises(ZeroDivisionError):
        trunc_div(1, 0)


def test_frozen_bundle_rejects_results_outside_declared_order():
    from engine_core.contracts import DataResult, DataStatus, FrozenDataBundle

    result = DataResult(
        request_id="r",
        function_id="extra",
        status=DataStatus.READY,
        data={"value": 1},
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=1,
        schema_version=1,
        completeness=1.0,
    )
    with pytest.raises(ValueError, match="unexpected DataResult"):
        FrozenDataBundle.from_results("eval", 1, (), {"extra": result})


def test_frozen_bundle_semantic_hash_excludes_provider_evidence():
    from dataclasses import replace
    from engine_core.contracts import DataResult, DataStatus, FrozenDataBundle, Provenance

    base = DataResult(
        request_id="r",
        function_id="previous_day_stats",
        status=DataStatus.READY,
        data={"previous_trade_date": "2026-09-03", "close": 1159},
        actual_source="td",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=1,
        schema_version=1,
        completeness=1.0,
        provenance=(Provenance("td", "td", "v1", "2026-09-03", 1, 1, "e1"),),
    )
    alternate_evidence = replace(
        base,
        actual_source="qmt",
        observed_at_ms=2,
        provenance=(Provenance("qmt", "http", "v2", "2026-09-03", 1, 2, "e2"),),
    )
    left = FrozenDataBundle.from_results("eval", 1, ("previous_day_stats",), {"previous_day_stats": base})
    right = FrozenDataBundle.from_results("eval", 1, ("previous_day_stats",), {"previous_day_stats": alternate_evidence})
    assert left.content_hash == right.content_hash
    assert left.submission_hash != right.submission_hash


def test_frozen_bundle_semantic_hash_contains_function_identity_and_derives_completeness():
    from engine_core.contracts import DataResult, DataStatus, FrozenDataBundle

    def result(function_id, completeness):
        return DataResult(
            request_id="r-" + function_id,
            function_id=function_id,
            status=DataStatus.READY,
            data={"value": function_id},
            actual_source="fixture",
            requested_trade_date="2026-09-04",
            actual_trade_date="2026-09-03",
            effective_at_ms=1,
            available_at_ms=1,
            observed_at_ms=1,
            schema_version=1,
            completeness=completeness,
        )

    first = result("a", 1.0)
    second = result("b", 0.25)
    bundle = FrozenDataBundle.from_results(
        "eval", 1, ("a", "b"), {"a": first, "b": second}
    )
    assert bundle.completeness == 0.25
    reordered = FrozenDataBundle.from_results(
        "eval", 1, ("b", "a"), {"a": first, "b": second}
    )
    assert bundle.content_hash != reordered.content_hash
    with pytest.raises(ValueError, match="bundle function keys"):
        FrozenDataBundle(
            evaluation_id="eval",
            knowledge_as_of_ms=1,
            results_by_function={"a": first},
            function_order=(),
        )


def test_data_result_hash_is_derived_from_immutable_semantics():
    from dataclasses import replace
    from engine_core.contracts import DataResult, DataStatus

    payload = {"previous_trade_date": "2026-09-03", "value": 1}
    result = DataResult(
        request_id="r",
        function_id="f",
        status=DataStatus.READY,
        data=payload,
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=1,
        schema_version=1,
        completeness=1.0,
    )
    original_hash = result.content_hash
    payload["value"] = 9
    assert result.data["value"] == 1
    with pytest.raises(TypeError):
        result.data["value"] = 2
    unavailable = replace(result, status=DataStatus.UNAVAILABLE)
    assert unavailable.content_hash != original_hash


def test_data_request_copies_mutable_sequences_at_the_contract_boundary():
    from dataclasses import FrozenInstanceError
    from engine_core.contracts import DataRequest

    symbols = ["600519"]
    required_fields = ["close_by_symbol"]
    request = DataRequest(
        request_id="r",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1,
        knowledge_as_of_ms=1,
        symbols=symbols,
        required_fields=required_fields,
    )
    symbols.append("000001")
    required_fields.append("amount_by_symbol")
    assert request.symbols == ("600519",)
    assert request.required_fields == ("close_by_symbol",)
    with pytest.raises(FrozenInstanceError):
        request.symbols += ("000001",)
    with pytest.raises(TypeError, match="ordered iterable"):
        DataRequest(
            request_id="r",
            function_id="f",
            trade_date="2026-09-04",
            effective_as_of_ms=1,
            knowledge_as_of_ms=1,
            symbols={"600519"},
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("observed_at_ms", 0),
        ("observed_at_ms", -1),
        ("observed_at_ms", True),
        ("effective_at_ms", 0),
        ("available_at_ms", -1),
    ),
)
def test_data_result_rejects_invalid_epoch_millisecond_contract(field_name, value):
    from engine_core.contracts import DataResult, DataStatus

    values = {
        "request_id": "r",
        "function_id": "f",
        "status": DataStatus.READY,
        "data": {"value": 1},
        "actual_source": "fixture",
        "requested_trade_date": "2026-09-04",
        "actual_trade_date": "2026-09-03",
        "effective_at_ms": 1,
        "available_at_ms": 1,
        "observed_at_ms": 1,
        "schema_version": 1,
        "completeness": 1.0,
    }
    values[field_name] = value
    with pytest.raises(ValueError, match=field_name):
        DataResult(**values)
