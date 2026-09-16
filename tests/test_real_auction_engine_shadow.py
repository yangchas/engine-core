from datetime import datetime
import importlib.util
from pathlib import Path


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
