import pytest

from engine_core.stock_day_facts import build_facts, read_facts, write_facts


def test_build_facts_joins_sources_without_zero_filling_and_stage_merges(tmp_path):
    auction = [{"symbol": "000001", "auction_tag": "0925", "chg_bp": "125",
                "match_amt_yuan": "900", "limit_state": "0"}]
    checkpoints = [{"symbol": "000001", "tag": "0945", "px": "11000",
                   "pre_close": "10000", "amt_yuan_cum": "1800", "last_tick_ts": "09:45:00"}]
    base = build_facts({"auction": auction, "checkpoints": checkpoints,
                        "names": [{"symbol": "000001", "name": "平安银行"}],
                        "yesterday_pool": [{"symbol": "000001", "lb_days": "1"}]},
                       trade_date="2026-10-15", stage="open")
    row = base[0]
    assert row["symbol"] == "000001"
    assert row["p0925_pct"] == 1.25
    assert row["p0925_cumulative_amount_yuan"] == 900
    assert row["p0945_pct"] == pytest.approx(10)
    assert row["p0930_pct"] is None
    assert row["yesterday_lb_days"] == 1
    assert "MISSING_DAILY_KLINE" not in row["missing_reason_codes"]

    close = build_facts({"daily": [{"symbol": "000001", "open": 10,
                                    "close": 10.5, "pre_close": 10,
                                    "amount_yuan": 10000}]},
                       trade_date="2026-10-15", stage="close", previous_rows=base)
    assert close[0]["p0945_pct"] == pytest.approx(10)
    assert close[0]["day_pct"] == pytest.approx(5)
    assert close[0]["delta_close_auction_pp"] == pytest.approx(3.75)
    manifest = write_facts(close, tmp_path, trade_date="2026-10-15", stage="close")
    loaded, loaded_manifest = read_facts(tmp_path)
    assert loaded[0]["symbol"] == "000001"
    assert loaded_manifest["sha256"] == manifest["sha256"]
    assert close[0]["revision"] == row["revision"] + 1


def test_duplicate_source_keys_are_deduplicated_by_latest_record_and_missing_name_is_not_code():
    rows = build_facts({"names": [{"symbol": "600001", "name": "旧名"},
                                  {"symbol": "600001", "name": "新名"}],
                        "checkpoints": [{"symbol": "600001", "tag": "0932", "px": 10000,
                                         "pre_close": 10000},
                                        {"symbol": "600001", "tag": "0932", "px": 10100,
                                         "pre_close": 10000},
                                        {"symbol": "600002", "tag": "0932", "px": 10000,
                                         "pre_close": 10000}]},
                       trade_date="2026-10-15", stage="open")
    assert len(rows) == 2
    assert rows[0]["name"] == "新名"
    assert rows[0]["p0932_pct"] == pytest.approx(1)
    assert rows[1]["display_name"] == "名称未知"
    assert "MISSING_NAME" in rows[1]["missing_reason_codes"]


def test_checkpoint_milli_units_and_multirow_relationship_join_are_preserved():
    rows = build_facts({
        "checkpoints": [{"symbol": "600001", "tag": "0945", "px": "11000",
                         "pre_close": "10000", "last_tick_ts": "09:45:00"}],
        "relations": [{"stockid": "600001", "plateid": "801001", "plate_name": "芯片"},
                      {"stockid": "600001", "plateid": "801002", "plate_name": "通信"}],
    }, trade_date="2026-10-15", stage="open")
    assert rows[0]["p0945_px_milli"] == 11000
    assert rows[0]["pre_close_milli"] == 10000
    assert rows[0]["plate_codes"] == ["801001", "801002"]
    assert rows[0]["plate_list_summary"] == ["芯片", "通信"]
    assert rows[0]["main_plate_name"] == "芯片"
    assert rows[0]["limit_pct"] == 10


def test_0925_checkpoint_fills_snapshot_price_holes_and_marks_indicative_tick():
    rows = build_facts({
        "auction": [{"symbol": "600001", "auction_tag": "0925", "ts": "09:25:06",
                     "match_amt_yuan": 500, "chg_bp": ""}],
        "checkpoints": [{"symbol": "600001", "tag": "0925", "last_tick_ts": "09:24:57",
                         "px": "9900", "pre_close": "10000", "amt_yuan_cum": "0"}],
    }, trade_date="2026-10-15", stage="open")
    assert rows[0]["p0925_pct"] == pytest.approx(-1)
    assert rows[0]["p0925_price_status"] == "INDICATIVE"
    assert rows[0]["p0925_source_id"] == "checkpoint_tick"
    assert rows[0]["p0925_cumulative_amount_yuan"] == 500


def test_checkpoint_preclose_stays_milli_when_snapshot_row_exists():
    rows = build_facts({
        "auction": [{"symbol": "600001", "auction_tag": "0920", "match_amt_yuan": 50}],
        "checkpoints": [{"symbol": "600001", "tag": "0920", "px": 11500,
                         "pre_close": 11000, "last_tick_ts": "09:20:00"}],
    }, trade_date="2026-10-15", stage="auction")
    assert rows[0]["pre_close_milli"] == 11000


def test_keyed_json_records_are_joined_by_symbol():
    rows = build_facts({"caps": {"600001": {"float_cap": 123, "total_cap": 456}}},
                       trade_date="2026-10-15", stage="auction")
    assert rows[0]["float_cap_yuan"] == 123
    assert rows[0]["total_cap_yuan"] == 456


def test_daily_kline_volume_alias_and_partial_row_are_truthfully_marked():
    rows = build_facts({"daily": [
        {"symbol": "600001", "open": 10, "high": 11, "low": 9,
         "close": 10.5, "pre_close": 10, "volume": 123456, "amount_yuan": 1000},
        {"symbol": "600002", "open": 10, "volume": 1},
    ]}, trade_date="2026-10-15", stage="close")
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["600001"]["day_volume_shares"] == 123456
    assert "MISSING_DAILY_KLINE" not in by_symbol["600001"]["missing_reason_codes"]
    assert "INCOMPLETE_DAILY_KLINE" in by_symbol["600002"]["missing_reason_codes"]
