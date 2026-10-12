import json
from pathlib import Path
from dataclasses import replace

import pytest

from engine_core import (
    DataResult,
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    WindowManager,
    WindowSpec,
)
from engine_core.auction_strategy import AuctionShadowStrategy
from engine_core.contracts import EngineSnapshot, semantic_hash


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture, name) -> EngineSnapshot:
    item = fixture["snapshots"][name]
    state = item["state"]
    metadata = {
        "source_table": item["source_table"],
        "business_anchor": item["trigger_id"],
        "business_anchor_time_ms": item["business_anchor_time_ms"],
        "source_record_time_ms": item["source_record_time_ms"],
    }
    content = {
        "snapshot_id": item["snapshot_id"],
        "trigger_id": item["trigger_id"],
        "logical_time_ms": item["business_anchor_time_ms"],
        "symbol": fixture["symbol"],
        "state": state,
    }
    return EngineSnapshot(
        snapshot_id=item["snapshot_id"],
        trigger_id=item["trigger_id"],
        logical_time_ms=item["business_anchor_time_ms"],
        session_id=fixture["trade_date"],
        phase="AUCTION",
        market_state_revision=1,
        source_observation_metadata=metadata,
        symbol_states={fixture["symbol"]: state},
        raw_market_cross_section={},
        raw_theme_cross_section={},
        windows={},
        coverage=1.0,
        completeness="READY",
        content_hash=semantic_hash(content),
        evidence_refs=tuple(fixture["evidence_refs"]),
    )


def test_strategy_is_pending_until_all_auction_anchors_exist():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    start = _snapshot(fixture, "pre_auction_0915")
    bundle = FrozenDataBundle.empty("eval-start", start.logical_time_ms)

    result = strategy.evaluate(start, bundle)

    assert result.state == "OBSERVE"
    assert result.trace["decision_status"] == "FACT_ONLY"
    assert result.trace["hash_contract_versions"]["submission"] == "SubmissionHashV1"
    assert result.trace["fact_status"] == "PENDING"
    assert result.trace["missing_trigger_ids"] == (
        "AUCTION_0920",
        "AUCTION_0924",
    )
    assert "BUY" not in str(result.trace)


def test_strategy_engine_boundary_composes_same_foundation_fact():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    snapshots = (
        _snapshot(fixture, "pre_auction_0915"),
        _snapshot(fixture, "auction_0920"),
        _snapshot(fixture, "auction_0924"),
    )
    result = None
    for index, snapshot in enumerate(snapshots):
        result = strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("eval-%d" % index, snapshot.logical_time_ms),
        )

    assert result is not None
    trace = result.trace
    shadow = trace["auction_fact_shadow"]
    assert trace["decision_status"] == "FACT_ONLY"
    assert trace["fact_status"] == "PARTIAL"
    assert shadow["scope_id"] == fixture["symbol"]
    assert shadow["status"] == "PARTIAL"
    assert shadow["metrics"]["price_delta_milli"] == -2060
    assert shadow["metrics"]["amount_delta_yuan"] == 4407516
    assert shadow["metrics"]["pressure_delta_yuan"] == 778730


def test_strategy_composes_legacy_theme_shadow_from_frozen_data_bundle():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        theme_delta_function_id="theme_auction_delta_compat",
    )
    theme_result = DataResult(
        request_id="theme-request",
        function_id="theme_auction_delta_compat",
        status=DataStatus.READY,
        data={
            "facts": (
                {
                    "theme_id": "theme-a",
                    "symbol_count": 1,
                    "amount_0925": 100.0,
                    "amount_delta_24_25": 60_000_000.0,
                    "amount_ratio_avg": 2.0,
                    "bid_amount_delta_24_25": 0.0,
                    "change_pct_delta_avg": 1.0,
                    "positive_delta_count": 1,
                    "evidence_refs": ("fixture://theme/a",),
                },
            )
        },
        actual_source="fixture",
        requested_trade_date=fixture["trade_date"],
        actual_trade_date=fixture["trade_date"],
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=1,
        schema_version=1,
        completeness=1.0,
    )
    bundle = lambda evaluation_id, logical_time_ms: FrozenDataBundle.from_results(
        evaluation_id=evaluation_id,
        knowledge_as_of_ms=logical_time_ms,
        function_order=("theme_auction_delta_compat",),
        results_by_function={"theme_auction_delta_compat": theme_result},
    )
    result = None
    for index, name in enumerate(("pre_auction_0915", "auction_0920", "auction_0924")):
        snapshot = _snapshot(fixture, name)
        result = strategy.evaluate(snapshot, bundle("theme-eval-%d" % index, snapshot.logical_time_ms))
    assert result is not None
    theme_shadow = result.trace["theme_delta_shadow"]
    assert theme_shadow["data_status"] is DataStatus.READY
    assert theme_shadow["shadow"]["signal_counts"] == {"增量转强": 1}
    assert result.evidence_refs == tuple(sorted(set(fixture["evidence_refs"]) | {"fixture://theme/a"}))


def test_strategy_does_not_promote_unavailable_theme_data_to_facts():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        theme_delta_function_id="theme_auction_delta_compat",
    )
    unavailable = DataResult(
        request_id="theme-request-unavailable",
        function_id="theme_auction_delta_compat",
        status=DataStatus.UNAVAILABLE,
        data={"facts": ({"theme_id": "must-not-run"},)},
        actual_source=None,
        requested_trade_date=fixture["trade_date"],
        actual_trade_date=None,
        effective_at_ms=None,
        available_at_ms=None,
        observed_at_ms=1,
        schema_version=1,
        completeness=0.0,
    )
    result = None
    for index, name in enumerate(("pre_auction_0915", "auction_0920", "auction_0924")):
        snapshot = _snapshot(fixture, name)
        bundle = FrozenDataBundle.from_results(
            evaluation_id="unavailable-theme-%d" % index,
            knowledge_as_of_ms=snapshot.logical_time_ms,
            function_order=("theme_auction_delta_compat",),
            results_by_function={"theme_auction_delta_compat": unavailable},
        )
        result = strategy.evaluate(snapshot, bundle)
    assert result is not None
    theme_shadow = result.trace["theme_delta_shadow"]
    assert theme_shadow["data_status"] is DataStatus.UNAVAILABLE
    assert theme_shadow["shadow"] is None
    assert theme_shadow["reason_codes"] == ("THEME_DATA_NOT_READY",)


def test_strategy_lifecycle_pending_to_ready_and_duplicate_anchor_is_idempotent():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="READY",
        current_coverage_status="READY",
    )
    snapshots = tuple(
        _snapshot(fixture, name)
        for name in ("pre_auction_0915", "auction_0920", "auction_0924")
    )

    results = [
        strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("lifecycle-%d" % index, snapshot.logical_time_ms),
        )
        for index, snapshot in enumerate(snapshots)
    ]

    assert [item.trace["fact_status"] for item in results] == [
        "PENDING",
        "PENDING",
        "PARTIAL",
    ]
    terminal_shadow = results[-1].trace["auction_fact_shadow"]

    duplicate = strategy.evaluate(
        snapshots[-1],
        FrozenDataBundle.empty("lifecycle-2", snapshots[-1].logical_time_ms),
    )

    assert duplicate.trace["fact_status"] == "PARTIAL"
    assert duplicate.trace["auction_fact_shadow"]["content_hash"] == terminal_shadow[
        "content_hash"
    ]
    assert tuple(strategy._snapshots) == (
        "PRE_AUCTION_0915",
        "AUCTION_0920",
        "AUCTION_0924",
    )


def test_strategy_repeated_execution_is_deterministic():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def run_once():
        strategy = AuctionShadowStrategy(
            scope_id=fixture["symbol"],
            previous_coverage_status="PARTIAL",
            current_coverage_status="READY",
        )
        result = None
        for index, name in enumerate(
            ("pre_auction_0915", "auction_0920", "auction_0924")
        ):
            snapshot = _snapshot(fixture, name)
            result = strategy.evaluate(
                snapshot,
                FrozenDataBundle.empty("eval-%d" % index, snapshot.logical_time_ms),
            )
        return result

    left = run_once()
    right = run_once()
    assert left.content_hash == right.content_hash
    assert left.trace["auction_fact_shadow"]["content_hash"] == right.trace[
        "auction_fact_shadow"
    ]["content_hash"]
    assert left.trace["auction_fact_shadow"]["evidence_hash"] == right.trace[
        "auction_fact_shadow"
    ]["evidence_hash"]


def test_strategy_result_exposes_all_anchor_evidence_refs():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    for index, name in enumerate(("pre_auction_0915", "auction_0920", "auction_0924")):
        snapshot = _snapshot(fixture, name)
        snapshot = replace(snapshot, evidence_refs=("fixture://anchor/%s" % index,))
        result = strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("eval-%d" % index, snapshot.logical_time_ms),
        )

    assert result is not None
    assert result.evidence_refs == (
        "fixture://anchor/0",
        "fixture://anchor/1",
        "fixture://anchor/2",
    )


def test_engine_uses_production_strategy_without_changing_wheel_result():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshots = tuple(
        _snapshot(fixture, name)
        for name in ("pre_auction_0915", "auction_0920", "auction_0924")
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy,
        session_id=fixture["trade_date"],
        phase="AUCTION",
    )
    for index, snapshot in enumerate(snapshots):
        evaluation_id = "auction-eval-%d" % index
        engine._register_evaluation(evaluation_id, snapshot, ())
        engine.submit(
            EngineSignal(
                "auction-data-%d" % index,
                snapshot.logical_time_ms,
                index,
                SignalKind.DATA_READY,
                {
                    "evaluation_id": evaluation_id,
                    "bundle": FrozenDataBundle.empty(
                        evaluation_id, snapshot.logical_time_ms
                    ),
                },
            )
        )

    result = engine.run_until_empty()
    assert len(result.strategy_results) == 3
    final_trace = result.strategy_results[-1].trace
    assert final_trace["strategy_id"] == "auction-shadow-v1"
    assert final_trace["decision_status"] == "FACT_ONLY"
    assert final_trace["auction_fact_shadow"]["status"] == "PARTIAL"
    assert final_trace["auction_fact_shadow"]["metrics"]["amount_delta_yuan"] == 4407516

    direct = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    direct_result = None
    for index, snapshot in enumerate(snapshots):
        direct_result = direct.evaluate(
            snapshot,
            FrozenDataBundle.empty("direct-%d" % index, snapshot.logical_time_ms),
        )
    assert direct_result is not None
    assert (
        final_trace["auction_fact_shadow"]["content_hash"]
        == direct_result.trace["auction_fact_shadow"]["content_hash"]
    )
    assert (
        final_trace["auction_fact_shadow"]["evidence_hash"]
        == direct_result.trace["auction_fact_shadow"]["evidence_hash"]
    )


def test_strategy_rejects_cross_session_and_conflicting_anchor_snapshots():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    strategy.evaluate(start, FrozenDataBundle.empty("eval-1", start.logical_time_ms))

    with pytest.raises(ValueError, match="conflicting snapshot"):
        changed = replace(start, content_hash=semantic_hash({"changed": True}))
        strategy.evaluate(
            changed,
            FrozenDataBundle.empty("eval-2", changed.logical_time_ms),
        )

    with pytest.raises(ValueError, match="mix sessions"):
        other = replace(start, session_id="2026-09-04")
        strategy.evaluate(
            other,
            FrozenDataBundle.empty("eval-3", other.logical_time_ms),
        )


def test_strategy_uses_later_timeline_revision_and_preserves_revision_history():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "auction_0920")
    middle = _snapshot(fixture, "auction_0924")
    end = replace(
        middle,
        snapshot_id="auction-0925",
        trigger_id="AUCTION_0925",
        logical_time_ms=middle.logical_time_ms + 66_000,
    )

    def revision_snapshot(snapshot, *, revision, logical_time_ms, content_hash):
        metadata = dict(snapshot.source_observation_metadata)
        metadata["auction_revision_ref"] = {
            "contract": "AuctionAnchorRevisionRefV1",
            "tag": snapshot.trigger_id.removeprefix("AUCTION_"),
            "revision": revision,
            "content_hash": content_hash,
            "supersedes_revision": revision - 1 if revision > 1 else None,
            "evaluation_time_ms": logical_time_ms,
        }
        return replace(
            snapshot,
            logical_time_ms=logical_time_ms,
            source_observation_metadata=metadata,
            content_hash=semantic_hash(
                {
                    "snapshot": snapshot.content_hash,
                    "revision": revision,
                    "logical_time_ms": logical_time_ms,
                }
            ),
        )

    start = revision_snapshot(
        start,
        revision=18,
        logical_time_ms=start.logical_time_ms + 20_000,
        content_hash="e" * 64,
    )
    first = revision_snapshot(
        middle,
        revision=10,
        logical_time_ms=middle.logical_time_ms,
        content_hash="a" * 64,
    )
    late = revision_snapshot(
        middle,
        revision=11,
        logical_time_ms=middle.logical_time_ms + 1_000,
        content_hash="b" * 64,
    )
    end = revision_snapshot(
        end,
        revision=1,
        logical_time_ms=end.logical_time_ms,
        content_hash="f" * 64,
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
    )

    for index, snapshot in enumerate((start, first, late, end)):
        strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("revision-%d" % index, snapshot.logical_time_ms),
        )

    result = strategy.evaluate(
        end,
        FrozenDataBundle.empty("revision-final", end.logical_time_ms),
    )
    revision_state = result.trace["auction_revision_state"]
    assert revision_state["consumed_revision_refs"]["AUCTION_0924"]["revision"] == 11
    assert revision_state["consumed_revision_refs"]["AUCTION_0920"]["revision"] == 18
    assert revision_state["consumed_revision_refs"]["AUCTION_0925"]["revision"] == 1
    assert revision_state["consumed_revision_refs"]["AUCTION_0924"]["content_hash"] == "b" * 64
    assert [
        item["timeline_revision_ref"]["revision"]
        for item in revision_state["revision_history"]["AUCTION_0924"]
    ] == [10, 11]
    assert revision_state["revision_history"]["AUCTION_0924"][1][
        "timeline_revision_ref"
    ]["supersedes_revision"] == 10
    assert result.trace["auction_fact_shadow"]["status"] in {"READY", "PARTIAL"}


def test_strategy_accepts_0925_supersession_and_rejects_invalid_revision_progression():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = _snapshot(fixture, "auction_0924")

    def revision_snapshot(*, revision, logical_time_ms, revision_hash):
        metadata = dict(base.source_observation_metadata)
        metadata["auction_revision_ref"] = {
            "contract": "AuctionAnchorRevisionRefV1",
            "tag": "0925",
            "revision": revision,
            "content_hash": revision_hash,
            "supersedes_revision": revision - 1 if revision > 1 else None,
            "evaluation_time_ms": logical_time_ms,
        }
        return replace(
            base,
            snapshot_id="auction-0925-revision-%d" % revision,
            trigger_id="AUCTION_0925",
            logical_time_ms=logical_time_ms,
            source_observation_metadata=metadata,
            content_hash=semantic_hash(
                {
                    "base": base.content_hash,
                    "revision": revision,
                    "logical_time_ms": logical_time_ms,
                    "revision_hash": revision_hash,
                }
            ),
        )

    first = revision_snapshot(
        revision=1,
        logical_time_ms=base.logical_time_ms + 66_000,
        revision_hash="a" * 64,
    )
    late = revision_snapshot(
        revision=2,
        logical_time_ms=first.logical_time_ms + 1_000,
        revision_hash="b" * 64,
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
    )
    strategy.evaluate(first, FrozenDataBundle.empty("first", first.logical_time_ms))
    result = strategy.evaluate(late, FrozenDataBundle.empty("late", late.logical_time_ms))

    revision_state = result.trace["auction_revision_state"]
    assert revision_state["consumed_revision_refs"]["AUCTION_0925"]["revision"] == 2
    assert [
        item["timeline_revision_ref"]["revision"]
        for item in revision_state["revision_history"]["AUCTION_0925"]
    ] == [1, 2]
    assert revision_state["revision_history"]["AUCTION_0925"][1][
        "timeline_revision_ref"
    ]["supersedes_revision"] == 1
    assert not any(
        item.startswith("REVISION_REF_MISMATCH")
        for item in revision_state["diagnostics"]
    )

    same_time_change = revision_snapshot(
        revision=3,
        logical_time_ms=late.logical_time_ms,
        revision_hash="c" * 64,
    )
    with pytest.raises(ValueError, match="conflicting snapshot"):
        strategy.evaluate(
            same_time_change,
            FrozenDataBundle.empty("same-time", same_time_change.logical_time_ms),
        )

    regressed = revision_snapshot(
        revision=1,
        logical_time_ms=late.logical_time_ms + 1_000,
        revision_hash="d" * 64,
    )
    with pytest.raises(ValueError, match="revision regressed"):
        strategy.evaluate(
            regressed,
            FrozenDataBundle.empty("regressed", regressed.logical_time_ms),
        )


def test_strategy_reports_revision_reference_mismatch_without_blocking_later_snapshot():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    middle = _snapshot(fixture, "auction_0924")
    first_metadata = dict(middle.source_observation_metadata)
    first_metadata["auction_revision_ref"] = {
        "contract": "AuctionAnchorRevisionRefV1",
        "tag": "0924",
        "revision": 4,
        "content_hash": "c" * 64,
        "supersedes_revision": 3,
    }
    first = replace(middle, source_observation_metadata=first_metadata)
    next_metadata = dict(first_metadata)
    next_metadata["auction_revision_ref"] = {
        **first_metadata["auction_revision_ref"],
        "revision": 4,
        "content_hash": "d" * 64,
        "supersedes_revision": 4,
    }
    later = replace(
        first,
        logical_time_ms=first.logical_time_ms + 1_000,
        source_observation_metadata=next_metadata,
        content_hash=semantic_hash({"later": True}),
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
    )

    strategy.evaluate(first, FrozenDataBundle.empty("first", first.logical_time_ms))
    result = strategy.evaluate(
        later,
        FrozenDataBundle.empty("later", later.logical_time_ms),
    )

    assert any(
        item.startswith("REVISION_REF_MISMATCH")
        for item in result.trace["auction_revision_state"]["diagnostics"]
    )
    assert result.trace["auction_revision_state"]["consumed_revision_refs"][
        "AUCTION_0924"
    ]["revision"] == 4


def test_strategy_rejects_revision_reference_regression():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    middle = _snapshot(fixture, "auction_0924")
    first_metadata = dict(middle.source_observation_metadata)
    first_metadata["auction_revision_ref"] = {
        "contract": "AuctionAnchorRevisionRefV1",
        "tag": "0924",
        "revision": 2,
        "content_hash": "e" * 64,
    }
    first = replace(middle, source_observation_metadata=first_metadata)
    regressed_metadata = dict(first_metadata)
    regressed_metadata["auction_revision_ref"] = {
        "contract": "AuctionAnchorRevisionRefV1",
        "tag": "0924",
        "revision": 1,
        "content_hash": "f" * 64,
    }
    later = replace(
        first,
        logical_time_ms=first.logical_time_ms + 1_000,
        source_observation_metadata=regressed_metadata,
        content_hash=semantic_hash({"regressed": True}),
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
    )

    strategy.evaluate(first, FrozenDataBundle.empty("first", first.logical_time_ms))
    with pytest.raises(ValueError, match="revision regressed"):
        strategy.evaluate(
            later,
            FrozenDataBundle.empty("later", later.logical_time_ms),
        )


def test_late_0920_revision_keeps_real_observation_order_and_partial_pair_facts():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "auction_0920")
    middle = _snapshot(fixture, "auction_0924")
    end = replace(
        middle,
        snapshot_id="auction-0925-late-order",
        trigger_id="AUCTION_0925",
        logical_time_ms=middle.logical_time_ms + 66_000,
    )

    def with_revision(snapshot, revision, logical_time_ms, content_hash):
        metadata = dict(snapshot.source_observation_metadata)
        metadata["auction_revision_ref"] = {
            "contract": "AuctionAnchorRevisionRefV1",
            "tag": snapshot.trigger_id.removeprefix("AUCTION_"),
            "revision": revision,
            "content_hash": content_hash,
            "supersedes_revision": revision - 1 if revision > 1 else None,
        }
        return replace(
            snapshot,
            logical_time_ms=logical_time_ms,
            source_observation_metadata=metadata,
            content_hash=semantic_hash(
                {
                    "base": snapshot.content_hash,
                    "revision": revision,
                    "logical_time_ms": logical_time_ms,
                }
            ),
        )

    first_start = with_revision(start, 1, start.logical_time_ms, "a" * 64)
    first_middle = with_revision(middle, 1, middle.logical_time_ms, "b" * 64)
    late_start = with_revision(
        start,
        2,
        middle.logical_time_ms + 20_000,
        "c" * 64,
    )
    final = with_revision(end, 1, end.logical_time_ms, "d" * 64)
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
        price_fields=(
            "auction_anchor_0920_price_milli",
            "auction_anchor_0924_price_milli",
            "auction_anchor_0925_price_milli",
        ),
    )

    for index, snapshot in enumerate(
        (first_start, first_middle, late_start, final)
    ):
        result = strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("late-order-%d" % index, snapshot.logical_time_ms),
        )

    assert result.trace["decision_status"] == "FACT_ONLY"
    assert result.trace["fact_status"] == "PARTIAL"
    assert (
        result.trace["auction_order_diagnostics"]["diagnostic_code"]
        == "ANCHOR_OBSERVATION_ORDER_NOT_COMPARABLE"
    )
    assert result.trace["auction_order_diagnostics"]["invalid_pairs"][0][
        "start_logical_time_ms"
    ] > result.trace["auction_order_diagnostics"]["invalid_pairs"][0][
        "end_logical_time_ms"
    ]
    assert "0924_to_0925" in result.trace["auction_fact_shadow"][
        "available_adjacent_segments"
    ]
    assert result.trace["auction_revision_state"]["consumed_revision_refs"][
        "AUCTION_0920"
    ]["revision"] == 2


def test_strategy_retains_only_declared_anchor_snapshots():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    strategy.evaluate(start, FrozenDataBundle.empty("eval-start", start.logical_time_ms))

    for index in range(100):
        unrelated = replace(
            start,
            trigger_id="UNRELATED_%03d" % index,
            snapshot_id="unrelated-%03d" % index,
        )
        strategy.evaluate(
            unrelated,
            FrozenDataBundle.empty("eval-unrelated-%03d" % index, unrelated.logical_time_ms),
        )

    assert tuple(strategy._snapshots) == ("PRE_AUCTION_0915",)
