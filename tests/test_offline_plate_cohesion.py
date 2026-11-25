import json
from types import SimpleNamespace

import pytest

from examples.run_offline_plate_cohesion import (
    _memberships, _read_td, ensure_empty_output_dir, select_top_plates, write_outputs,
)
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
    assert manifest["legacy_window_semantics"] == "PHASE_MIXED_LEGACY_WINDOW"


def test_writer_emits_phase_named_outputs(tmp_path):
    output = tmp_path / "out"
    phase_result = SimpleNamespace(
        by_phase={"AUCTION": ()}, affinities_by_phase={"AUCTION": ()},
        flags={"phase_sample_counts": {"AUCTION": 3}, "gap_skipped_points": 100, "gap": "GAP",
               "snapshot_crosscheck": {"0920": {"compared": 4, "equal": 3}},
               "auction_sub_phases": {"CANCELLABLE": ["09:15:00", "09:20:00"]}},
        content_hash="abc",
    )
    write_outputs(output, trade_date="2026-01-02", snapshot_path=tmp_path / "snap.json",
                  window_start="09:15:00", window_end="09:45:00", max_plates=30,
                  result=None, selected_plates=[], auction_row_count=0, tick_row_count=0,
                  series={}, pre_close={}, phase_result=phase_result, phases=("AUCTION",))
    assert {path.name for path in output.iterdir()} == {
        "cohesion_auction.json", "affinity_auction.json", "manifest.json"}
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["phase_sample_counts"] == {"AUCTION": 3}
    assert manifest["snapshot_crosscheck"]["0920"] == {"compared": 4, "equal": 3}


def test_td_reader_selects_and_derives_auction_book_fields(monkeypatch):
    memberships, hierarchy = _memberships({"stock_plate": {}, "s2p": {"000001": ["人工智能"]}})
    stamp = __import__("datetime").datetime.fromisoformat("2026-10-13T09:20:00+08:00")

    class Cursor:
        query = ""
        def execute(self, query):
            self.query = query
        def fetchall(self):
            if "auction_snapshot_v2" in self.query:
                return [("000001", 774100, 18423580)] if '"0920"' in self.query else []
            if "LAST(pc_milli)" in self.query:
                return [(774100, "000001")]
            return [(stamp, 760000, 774100, 0, 0, 774100, 774100,
                     238, 238, 0, 50, "000001")]

    class Connection:
        def cursor(self):
            self.cursor_value = Cursor()
            return self.cursor_value
        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr("examples.run_offline_plate_cohesion._td_connect",
                        lambda host: (connection, "market_data1"))
    _, series, _, _, _, _, _ = _read_td("local", "2026-10-13", "09:15:00", "09:25:00",
                                         memberships, hierarchy, 1)
    assert "ap1_milli" in connection.cursor_value.query and "bv2" in connection.cursor_value.query
    assert series["000001"][0][1:3] == (774.1, 18423580.0)
