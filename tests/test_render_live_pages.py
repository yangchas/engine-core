import json
import csv
from datetime import date, timedelta

import pytest

from examples.render_live_pages import render_page, write_page


def _history_csv(path, days=22):
    fields = ["date", "symbol", "open", "high", "low", "close", "volume", "amount_yuan", "turnover_pct", "pct_chg"]
    start = date(2026, 9, 1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        previous_a = None
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            # A was limit-up on the last two prior sessions; B has a rising 20-day range.
            close_a = (previous_a * 1.1) if i >= days - 2 and previous_a is not None else 10 + i * .1
            previous_a = close_a
            for symbol, close, pct in (("000001", close_a, 10 if i >= days - 2 else 0),
                                       ("000002", 20 + i * .2, 0)):
                writer.writerow({"date": day, "symbol": symbol, "open": close * (1 + (-.01 if i == days - 1 else .01)),
                                 "high": close, "low": close * .9, "close": close,
                                 "volume": "", "amount_yuan": 1000000, "turnover_pct": "", "pct_chg": pct})


def _state(symbol, pct, amount, *, ts="09:25:00", limit_state=0, rb=0, ra=0):
    price = int(100000 * (1 + pct / 100))
    return {"symbol": symbol, "pct_change": pct, "match_amount_yuan": amount,
            "price_milli": price, "prev_close_milli": 100000,
            "rest_bid_shares": rb, "rest_ask_shares": ra,
            "rest_bid_yuan": rb * 100, "rest_ask_yuan": ra * 100,
            "limit_state": limit_state, "ts": ts}


def test_render_page_has_sections_and_self_contained_assets():
    td = type("TD", (), {"states_at": lambda self, cutoff: {}})()
    page, payload = render_page(td, trade_date="2026-10-15", stage="auction_0926", previous_day_final_yuan=None)
    for section in ("一屏结论", "大盘", "板块份额", "封涨停质量", "昨日连板高标", "竞价内变化", "看A做B", "近72日对比"):
        assert section in page
    assert "<style>" in page and "@media" in page
    assert payload["limitations"]


def test_write_page_refuses_nonempty_directory(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(FileExistsError):
        write_page(output, "page", {})


def test_history_drives_conclusion_highboard_and_peer_sections(tmp_path):
    history = tmp_path / "daily.csv"
    _history_csv(history)
    class TD:
        def states_at(self, cutoff):
            minute = cutoff.strftime("%H:%M")
            p_a = -8 if minute < "09:32" else (-7 if minute < "09:40" else -9)
            return {"000001": _state("000001", p_a, 50000000, ts=cutoff.strftime("%H:%M:%S")),
                    "000002": _state("000002", -4 if minute < "09:32" else -2, 30000000, ts=cutoff.strftime("%H:%M:%S"))}
        def latest_tick_ts(self): return "2026-10-15T09:45:30"
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="open_0946",
                                relations={"p1": {"000001", "000002"}}, plate_names={"p1": "测试板块"},
                                daily_history_csv=history, stock_names={"000001": "高标", "000002": "同伴"})
    assert payload["conclusion"] and 3 <= len(payload["conclusion"]) <= 5
    assert payload["history"]["median_is_historical_low"] is True
    assert payload["history"]["history_min_median_date"]
    assert payload["chapters"]["yesterday_highboards"][0]["streak"] == 2
    assert payload["chapters"]["yesterday_highboards"][0]["status"] in {"封死", "开板", "回封", "走弱", "观察"}
    assert payload["chapters"]["look_a_do_b"]
    assert "昨日连板高标" in page and "近72日对比" in page


def test_c155_behavior_tags_and_seal_quality_are_rendered(tmp_path):
    class TD:
        def states_at(self, cutoff):
            if cutoff.strftime("%H:%M") == "09:24":
                return {"000001": _state("000001", 1, 10000000), "000002": _state("000002", 0, 10000000)}
            return {"000001": _state("000001", 4, 50000000, limit_state=1, rb=100),
                    "000002": _state("000002", -2, 10000000)}
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    assert payload["chapters"]["behavior_tags"]["direction_counts"]["价升份额升(抢筹候选)"] == 1
    assert payload["chapters"]["seal_quality"]["up_count"] == 1
    assert "价升份额升(抢筹候选)" in page and "封涨停质量" in page


def test_missing_history_is_explicit_and_cli_inputs_are_supported():
    class TD:
        def states_at(self, cutoff): return {}
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    assert payload["history"] is None
    assert payload["chapters"]["look_a_do_b"] == "缺少输入"
    assert payload["chapters"]["yesterday_highboards"] == "缺少输入"
    assert "缺少输入" in page
