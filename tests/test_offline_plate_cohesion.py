import json

import pytest

from examples.run_offline_plate_cohesion import _memberships, ensure_empty_output_dir, select_top_plates, write_outputs
from engine_core.plate_cohesion import PlateCohesionResult
from engine_core.plate_membership import PlateHierarchy


def test_nonempty_output_directory_is_rejected(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(ValueError, match="empty"):
        ensure_empty_output_dir(output)


def test_top_plates_use_0925_amount_and_only_theme_kind():
    memberships, hierarchy = _memberships({
        "stock_plate": {}, "s2p": {"a": ["人工智能", "回购"], "b": ["人工智能"], "c": ["低空经济"]},
    })
    selected = select_top_plates(memberships, hierarchy, {
        "a": {"match_amt_yuan": 10}, "b": {"match_amt_yuan": 5}, "c": {"match_amt_yuan": 50},
    }, 2)
    assert selected == ["低空经济", "人工智能"]


def test_writer_emits_facts_manifest(tmp_path):
    output = tmp_path / "out"
    result = PlateCohesionResult((), (), 3000, 2)
    write_outputs(output, trade_date="2026-01-02", snapshot_path=tmp_path / "snap.json",
                  window_start="09:20:00", window_end="09:45:00", max_plates=30,
                  result=result, selected_plates=["人工智能"], auction_row_count=2,
                  tick_row_count=0, series={"a": []}, pre_close={})
    assert {path.name for path in output.iterdir()} == {"cohesion.json", "affinity.json", "manifest.json"}
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["interpretation"] == "离线事实, 非策略结论"
    assert manifest["missing_counts"]["symbols_without_tick_rows"] == 1
    assert manifest["window_ms"] == 30000 and manifest["max_lag_points"] == 10
