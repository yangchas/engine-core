from datetime import datetime
import importlib.util
from pathlib import Path

from engine_core import (
    AUCTION_REFERENCE_FUNCTION_ORDER,
    AuctionReferencePreparation,
    DataResult,
    DataStatus,
    local_datetime_ms,
)


SPEC = importlib.util.spec_from_file_location(
    "real_auction_engine_shadow",
    Path(__file__).parents[1] / "examples" / "run_real_auction_engine_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _rows():
    return [
        (datetime(2026, 9, 9, 9, 20, 3, 83000), 1305000, -32, 10831500, 0, 2740500, 0, "600519", "20260909", "0920"),
        (datetime(2026, 9, 9, 9, 24, 10, 110000), 1305000, -32, 21532500, 5089500, 0, 0, "600519", "20260909", "0924"),
        (datetime(2026, 9, 9, 9, 25, 6, 81000), 1305010, -32, 33408300, 130500, 130501, 0, "600519", "20260909", "0925"),
    ]


def _prepared_references():
    cutoff = local_datetime_ms("2026-09-09", "09:19:00")
    results = []
    for function_id in AUCTION_REFERENCE_FUNCTION_ORDER:
        results.append(
            (
                function_id,
                DataResult(
                    request_id="%s-request" % function_id,
                    function_id=function_id,
                    status=DataStatus.READY,
                    data={"source": "fixture", "function": function_id},
                    actual_source="fixture",
                    requested_trade_date="2026-09-09",
                    actual_trade_date="2026-09-08",
                    effective_at_ms=cutoff,
                    available_at_ms=cutoff,
                    observed_at_ms=cutoff,
                    schema_version=1,
                    completeness=1.0,
                ),
            )
        )
    return AuctionReferencePreparation(
        trade_date="2026-09-09",
        previous_trade_date="2026-09-08",
        knowledge_as_of_ms=cutoff,
        results=tuple(results),
    )


def test_real_projection_traverses_public_engine_signal_path_and_matches_fact_wheel():
    result = MODULE.run_engine_shadow(
        rows=_rows(), trade_date="2026-09-09", symbol="600519"
    )

    assert result["read_only"] is True
    assert result["processed_signals"] == 6
    assert result["strategy_result_count"] == 3
    assert result["engine_fact_only"] is True
    assert result["engine_fact_status"] == "PARTIAL"
    assert result["semantic_hash_equal"] is True
    assert len(result["engine_strategy_evidence_refs"]) == 3
    assert result["snapshot_source_time_range"]["0920"]["oldest"] == 1788916803083


def test_prepared_references_bind_through_engine_data_ready_path():
    result = MODULE.run_engine_shadow(
        rows=_rows(),
        trade_date="2026-09-09",
        symbol="600519",
        preparation=_prepared_references(),
    )

    assert result["read_only"] is True
    assert result["reference_binding"] == "ENGINE_DATA_READY"
    assert len(result["reference_bundle_hashes"]) == 3
    assert result["processed_signals"] == 9
    assert result["strategy_result_count"] == 3
    assert result["semantic_hash_equal"] is True
