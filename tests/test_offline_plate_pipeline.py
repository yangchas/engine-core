import json

import pytest

from examples.run_offline_plate_pipeline import build_day, ensure_empty_output_dir


def _kline(symbol, day, close, pct):
    return [day, 1, close, close, close, 10, 100, 1, pct]


def test_day_pipeline_tier_priority_and_auction_aggregation():
    # 2026-01-02 is a 10% limit-up close, so it is the prior-day tier-1 pool.
    klines = {"a": [_kline("a", "2026-01-01", 10, 0), _kline("a", "2026-01-02", 11, 10),
                     _kline("a", "2026-01-05", 11, 0)],
              "b": [_kline("b", "2026-01-01", 10, 0), _kline("b", "2026-01-02", 10, 0),
                     _kline("b", "2026-01-05", 10, 0)]}
    snapshot = {"stock_plate": {"a": "静态板", "b": "乙板"}, "s2p": {"a": ["静态板", "乙板"], "b": ["乙板"]}}
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol=klines,
        auction_rows={"a": {"px_milli": 11000, "match_amt_yuan": 300, "limit_state": 1},
                      "b": {"px_milli": 10000, "match_amt_yuan": 200, "limit_state": 0}},
        snapshot=snapshot, reasons_by_symbol={}, reason_asof_unknown=False,
    )
    memberships = result["memberships"]
    assert memberships.by_symbol["a"][0].tier == 3
    assert memberships.by_symbol["a"][0].plate == "静态板"
    stats = {row.plate: row for row in result["aggregation"].stats}
    assert stats["静态板"].amount_sum == 300
    assert stats["静态板"].limit_up_count == 1
    assert result["quality"]["yest_pool_member_count"] == 1


def test_no_plate_is_counted_and_never_filled_with_other():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000, "match_amt_yuan": None, "limit_state": None}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={}, reason_asof_unknown=False,
        min_auction_rows=1,
    )
    assert result["quality"]["unmapped_auction_symbol_count"] == 1
    assert "其他" not in {row.plate for rows in result["memberships"].by_symbol.values() for row in rows}
    assert result["quality"]["missing_counts"]["match_amt_yuan"] == 1


def test_reason_without_date_is_flagged_unknown():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000, "match_amt_yuan": 100, "limit_state": 0}},
        snapshot={"stock_plate": {}, "s2p": {}},
        reasons_by_symbol={"a": [{"reason": "人工智能"}]}, reason_asof_unknown=True,
        min_auction_rows=1,
    )
    assert result["reason_asof_unknown"] is True
    assert any(row.tier == 2 for row in result["memberships"].by_symbol["a"])


def test_output_directory_must_be_empty(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing.json").write_text("{}")
    with pytest.raises(ValueError, match="empty"):
        ensure_empty_output_dir(output)


def test_insufficient_day_is_recorded_as_skipped(tmp_path):
    from examples.run_offline_plate_pipeline import run_pipeline

    days = run_pipeline(
        dates=["2026-01-05"], klines_by_symbol={}, auction_by_date={"2026-01-05": {}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        start_date="2026-01-05", end_date="2026-01-05", min_auction_rows=2,
    )
    assert days["skipped"] == [{"date": "2026-01-05", "reason": "auction_row_count_below_minimum", "auction_row_count": 0}]


def test_kaipan_pool_input_owns_tier_one_membership(tmp_path):
    from examples.run_offline_plate_pipeline import _load_kaipan_inputs, run_pipeline

    pool_dir = tmp_path / "kaipan" / "pools"
    pool_dir.mkdir(parents=True)
    row = ["000001", "平安银行"] + [None] * 10 + ["Kaipan板"] + [None, None, 3]
    (pool_dir / "2026-01-02_ban1.json").write_text(
        json.dumps({"info": [[row], ["2026-01-02", "metadata"]], "errcode": "0"}),
        encoding="utf-8",
    )
    pools, hierarchy = _load_kaipan_inputs(tmp_path / "kaipan")
    result = run_pipeline(
        dates=["2026-01-05"], klines_by_symbol={
            "000001": [_kline("000001", "2026-01-02", 11, 10),
                       _kline("000001", "2026-01-05", 11, 0)]},
        auction_by_date={"2026-01-05": {"000001": {"px_milli": 11000,
                                                       "match_amt_yuan": 50,
                                                       "limit_state": 1}}},
        snapshot={"stock_plate": {"000001": "静态板"}, "s2p": {}},
        reasons_by_symbol={}, start_date="2026-01-05", end_date="2026-01-05",
        min_auction_rows=1, kaipan_pool_by_day=pools, kaipan_hierarchy=hierarchy,
    )
    value = result["days"][0][1]
    assert value["memberships"].by_symbol["000001"][0].plate == "Kaipan板"
    assert value["memberships"].by_symbol["000001"][0].tier == 1
    assert value["quality"]["pool_source"] == "kaipan_pool"


def test_kaipan_reasons_are_filtered_as_of_and_recent_candidates_are_first():
    from examples.run_offline_plate_pipeline import _kaipan_reasons_for_day, _reason_candidates

    reasons, stats = _kaipan_reasons_for_day({"000001": [
        {"Date": "2026-01-05", "Reason": "旧板(旧题材)"},
        {"Date": "2026-01-07", "Reason": "未来板(未来题材)"},
        {"Date": "2026-01-06", "Reason": "新板(新题材)"},
    ]}, "2026-01-06", {"000001"})
    assert [row["reason"] for row in reasons["000001"]] == ["新板(新题材)", "旧板(旧题材)"]
    assert _reason_candidates(reasons)["000001"][:2] == ["新板", "旧板"]
    assert stats == {"used": 2, "excluded_future": 1, "excluded_no_date": 0,
                     "symbol_count_used": 1, "asof_enabled": True, "type0": 0, "type1": 0}


def test_kaipan_type_zero_candidates_precede_type_one_and_are_labeled():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"000001": {"px_milli": 10000}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        reason_asof_unknown=False, min_auction_rows=1,
        kaipan_reasons_by_symbol={"000001": [
            {"Date": "2026-01-05", "Type": "1", "Reason": "异动板(异动主题)"},
            {"Date": "2026-01-04", "Type": "0", "Reason": "涨停板(涨停主题)"},
        ]},
    )
    rows = result["memberships"].by_symbol["000001"]
    reason_rows = [row for row in rows if row.tier == 2]
    assert [row.plate for row in reason_rows][:2] == ["涨停板", "异动板"]
    assert "reason_limit_up" in reason_rows[0].source
    assert any("reason_move" in row.source for row in reason_rows)
    assert result["quality"]["reason_item_count_type0"] == 1
    assert result["quality"]["reason_item_count_type1"] == 1


def test_previous_day_type_zero_reasons_rebuild_pool_when_no_pool_file():
    from examples.run_offline_plate_pipeline import run_pipeline

    result = run_pipeline(
        dates=["2026-01-05"], klines_by_symbol={"000001": [
            _kline("000001", "2026-01-02", 10, 0), _kline("000001", "2026-01-05", 10, 0)]},
        auction_by_date={"2026-01-05": {"000001": {"px_milli": 10000}}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        start_date="2026-01-05", end_date="2026-01-05", min_auction_rows=1,
        kaipan_reasons_by_symbol={"000001": [
            {"Date": "2026-01-02", "Type": "0", "Reason": "新材料(固态电池)"},
            {"Date": "2026-01-02", "Type": "1", "Reason": "异动板(异动题材)"},
        ]},
    )["days"][0][1]
    assert result["quality"]["pool_source"] == "kaipan_reasons"
    assert result["yest_pool_symbols"] == {"000001"}
    assert result["yest_pool_members"]["000001"]["plate"] == "新材料"
    assert result["yest_pool_members"]["000001"]["lb_days"] is None
    assert result["quality"]["kaipan_reasons_only"] == 1
    assert result["quality"]["td_only"] == 0


def test_existing_kaipan_pool_file_wins_over_reason_reconstruction():
    from engine_core.plate_hierarchy_kaipan import PoolRow
    from examples.run_offline_plate_pipeline import run_pipeline

    result = run_pipeline(
        dates=["2026-01-05"], klines_by_symbol={"000001": [
            _kline("000001", "2026-01-02", 10, 0), _kline("000001", "2026-01-05", 10, 0)]},
        auction_by_date={"2026-01-05": {"000001": {"px_milli": 10000}}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        start_date="2026-01-05", end_date="2026-01-05", min_auction_rows=1,
        kaipan_pool_by_day={"2026-01-02": {"000001": PoolRow("文件板", 2)}},
        kaipan_reasons_by_symbol={"000002": [
            {"Date": "2026-01-02", "Type": "0", "Reason": "原因板(原因题材)"},
        ]},
    )["days"][0][1]
    assert result["quality"]["pool_source"] == "kaipan_pool"
    assert result["yest_pool_symbols"] == {"000001"}
    assert result["yest_pool_members"]["000001"]["plate"] == "文件板"


def test_kaipan_reasons_without_valid_date_are_excluded_and_counted():
    from examples.run_offline_plate_pipeline import _kaipan_reasons_for_day

    reasons, stats = _kaipan_reasons_for_day({"000001": [
        {"Date": "not-a-date", "Reason": "坏日期(题材甲)"},
        {"Reason": "缺日期(题材乙)"},
    ]}, "2026-01-06", {"000001"})
    assert reasons == {}
    assert stats == {"used": 0, "excluded_future": 0, "excluded_no_date": 2,
                     "symbol_count_used": 0, "asof_enabled": True, "type0": 0, "type1": 0}


def test_kaipan_reason_asof_applies_to_symbols_outside_yesterday_pool(tmp_path):
    from examples.run_offline_plate_pipeline import _load_kaipan_reasons

    reason_dir = tmp_path / "kaipan" / "reasons"
    reason_dir.mkdir(parents=True)
    (reason_dir / "a.json").write_text(json.dumps({
        "StockID": "a", "List": [{"Date": "2026-01-04", "Reason": "新材料(固态电池)"}],
    }), encoding="utf-8")
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        reason_asof_unknown=False, min_auction_rows=1,
        kaipan_reasons_by_symbol=_load_kaipan_reasons(tmp_path / "kaipan"),
    )
    assert any(row.tier == 2 and row.plate == "新材料" for row in result["memberships"].by_symbol["a"])
    assert result["quality"]["reason_source_counts"] == {
        "kaipan_asof": 1, "kaipan_unavailable_asof": 0, "legacy_snapshot": 0,
    }


def test_future_only_kaipan_reason_does_not_fall_back_to_legacy_snapshot(tmp_path):
    from examples.run_offline_plate_pipeline import _load_kaipan_reasons

    reason_dir = tmp_path / "kaipan" / "reasons"
    reason_dir.mkdir(parents=True)
    (reason_dir / "a.json").write_text(json.dumps({
        "StockID": "a", "List": [{"Date": "2026-01-06", "Reason": "未来板(未来题材)"}],
    }), encoding="utf-8")
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000}},
        snapshot={"stock_plate": {}, "s2p": {}},
        reasons_by_symbol={"a": [{"reason": "未来不可用"}]},
        reason_asof_unknown=True, min_auction_rows=1,
        kaipan_reasons_by_symbol=_load_kaipan_reasons(tmp_path / "kaipan"),
    )
    assert not any(row.tier == 2 for row in result["memberships"].by_symbol["a"])
    assert result["quality"]["reason_source_counts"] == {
        "kaipan_asof": 0, "kaipan_unavailable_asof": 1, "legacy_snapshot": 0,
    }
    assert result["quality"]["reason_symbol_count_asof_unavailable"] == 1
    assert result["reason_asof_unknown"] is False


def test_kaipan_hierarchy_is_rebuilt_as_of_each_day():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"000001": {"px_milli": 10000, "match_amt_yuan": 1}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        reason_asof_unknown=False, min_auction_rows=1,
        kaipan_reasons_by_symbol={"000001": [{
            "Date": "2026-01-06", "Reason": "未来(未来板)",
            "ZSCode": ["801001"], "PZSCode": "803001",
        }]},
    )
    assert result["quality"]["hierarchy_edge_count"] == 0
    assert result["quality"]["hierarchy_items_used"] == 0
    assert result["quality"]["hierarchy_items_excluded_future"] == 1


def test_missing_kaipan_reason_file_falls_back_to_legacy_snapshot_and_counts_unknown():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000}},
        snapshot={"stock_plate": {}, "s2p": {}},
        reasons_by_symbol={"a": [{"reason": "旧快照主题"}]},
        reason_asof_unknown=False, min_auction_rows=1,
        kaipan_reasons_by_symbol={},
    )
    assert any(row.tier == 2 and row.plate == "旧快照主题" for row in result["memberships"].by_symbol["a"])
    assert result["quality"]["reason_source_counts"] == {
        "kaipan_asof": 0, "kaipan_unavailable_asof": 0, "legacy_snapshot": 1,
    }
    assert result["quality"]["reason_asof_unknown_symbol_count"] == 1
    assert result["reason_asof_unknown"] is True



def test_kline_by_day_accepts_datetime_ts():
    import datetime as _dt
    from examples.run_offline_plate_pipeline import _kline_by_day

    row = (_dt.datetime(2026, 10, 12), 1, 1, 1, 1, 1, 1, 1, 0)
    assert _kline_by_day({"000001": [row]}) == {"2026-10-12": {"000001": row}}


def test_td_reader_only_swallows_explicit_missing_symbol_tables(monkeypatch, tmp_path):
    from argparse import Namespace
    from examples import run_offline_plate_pipeline as module

    class Cursor:
        def __init__(self, message):
            self.message = message

        def execute(self, query):
            if "FROM d_000001" in query:
                raise RuntimeError(self.message)
            if "FROM d_000002" in query:
                error = RuntimeError("TD error")
                error.errno = 0x2603
                raise error

        def fetchall(self):
            return []

    class Connection:
        def __init__(self, message):
            self.cursor_value = Cursor(message)

        def cursor(self):
            return self.cursor_value

        def close(self):
            pass

    current = {"connection": Connection("Table does not exist: d_000001")}
    monkeypatch.setattr(module, "_connect_td", lambda _host: (current["connection"], "db"))
    loaded = module._read_td("host", "db", ["000001", "000002"], "2026-01-01", "2026-01-02", [])
    assert loaded[3] == 2

    current["connection"] = Connection("permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        module._read_td("host", "db", ["000001"], "2026-01-01", "2026-01-02", [])

    day = "2026-01-05"
    value = module.build_day(
        trade_date=day, klines_by_symbol={}, auction_rows={"000001": {}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        reason_asof_unknown=False, min_auction_rows=1, skipped_kline_table_symbol_count=1,
    )
    output = tmp_path / "out"
    output.mkdir()
    module._emit(output, {"days": [(day, value)], "skipped": []},
                 Namespace(start_date=day, end_date=day, reasons=None, legacy_pool_dir=None))
    assert json.loads((output / day / "day_quality.json").read_text())["skipped_kline_table_symbol_count"] == 1
    assert json.loads((output / "summary.json").read_text())["skipped_kline_table_symbol_count_by_date"] == {day: 1}


def test_membership_artifact_preserves_kind_for_each_member(tmp_path):
    from argparse import Namespace
    from examples.run_offline_plate_pipeline import _emit

    day = "2026-01-05"
    value = build_day(
        trade_date=day, klines_by_symbol={}, auction_rows={"a": {"px_milli": 10000}},
        snapshot={"stock_plate": {"a": "业绩增长"}, "s2p": {}}, reasons_by_symbol={},
        reason_asof_unknown=False, min_auction_rows=1,
    )
    output = tmp_path / "out"
    output.mkdir()
    _emit(output, {"days": [(day, value)], "skipped": []}, Namespace(
        start_date=day, end_date=day, reasons=None, legacy_pool_dir=None,
    ))
    artifact = json.loads((output / day / "memberships.json").read_text())
    member = artifact["by_symbol"]["a"][0]
    assert member["kind"] == "EVENT"


def test_membership_artifact_exports_legacy_v1_primary_used_by_match_rate(tmp_path):
    from argparse import Namespace
    from examples.run_offline_plate_pipeline import _emit

    day = "2026-01-05"
    value = build_day(
        trade_date=day, klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000}, "b": {"px_milli": 10000}},
        snapshot={"stock_plate": {"a": "甲板", "b": "乙板"}, "s2p": {}},
        reasons_by_symbol={}, reason_asof_unknown=False, min_auction_rows=1,
    )
    output = tmp_path / "out"
    output.mkdir()
    _emit(output, {"days": [(day, value)], "skipped": []}, Namespace(
        start_date=day, end_date=day, reasons=None, legacy_pool_dir=None,
    ))

    artifact = json.loads((output / day / "memberships.json").read_text())
    quality = json.loads((output / day / "day_quality.json").read_text())
    legacy_primary = artifact["legacy_v1_primary_by_symbol"]
    current_primary = {
        symbol: rows[0]["plate"] for symbol, rows in artifact["by_symbol"].items() if rows
    }
    comparable = set(current_primary) & set(legacy_primary)
    assert legacy_primary == {"a": "甲板", "b": "乙板"}
    assert quality["legacy_v1_primary_count"] == len(legacy_primary)
    assert quality["legacy_v1_primary_match_count"] == sum(
        current_primary[symbol] == legacy_primary[symbol] for symbol in comparable
    )
