from engine_core.auction_tracking import Tick
from engine_core.bar_indicators import build_bar_report, detect_turning_points, summarize_curve


def test_bars_use_window_amount_increment_and_final_print_after_0925():
    ticks = [
        Tick("2026-10-14", "09:24:00", "600000", pc_milli=10000,
             bp1_milli=10000, ap1_milli=10000, bv1=10, av1=10),
        Tick("2026-10-14", "09:25:00", "600000", px_milli=10100, pc_milli=10000,
             bp1_milli=10100, ap1_milli=10100, bv1=1, av1=1, amt_yuan=50500),
    ]
    report = build_bar_report(trade_date="2026-10-14", auction_ticks=ticks)
    market = [b for b in report["bars"] if b["object_type"] == "market" and b["window_minutes"] == 1]
    final = next(b for b in market if b["timestamp"].endswith("09:25:00"))
    assert final["window_amount_yuan"] == 50500 - 10000
    assert round(final["window_pct_change_pp"], 6) == 1.0


def test_turning_points_filter_small_extrema_and_find_acceleration_and_flip(monkeypatch):
    import engine_core.bar_indicators as bars
    monkeypatch.setattr(bars, "PEAK_MIN_AMOUNT_YUAN", 20)
    rows = [
        {"timestamp": f"2026-10-14T09:2{i}:00", "window_amount_yuan": amount,
         "window_pct_change_pp": pct}
        for i, (amount, pct) in enumerate([(10, -1), (25, -0.8), (80, 0.7), (20, 0.8), (15, -0.9)])
    ]
    points = detect_turning_points(rows)
    assert any(p["kind"] == "ACCELERATION_START" and p["timestamp"].endswith("09:21:00") for p in points)
    assert any(p["kind"] == "DIRECTION_FLIP" and p["timestamp"].endswith("09:22:00") for p in points)
    assert any(p["kind"] == "AMOUNT_PEAK" and p["timestamp"].endswith("09:22:00") for p in points)
    assert not any(p["timestamp"].endswith("09:21:00") and p["kind"] == "AMOUNT_PEAK" for p in points)


def test_t50_last_minute_and_rule_labels():
    rows = [
        {"timestamp": "2026-10-14T09:23:00", "phase": "AUCTION", "cumulative_amount_yuan": 40},
        {"timestamp": "2026-10-14T09:24:00", "phase": "AUCTION", "cumulative_amount_yuan": 60},
        {"timestamp": "2026-10-14T09:25:00", "phase": "AUCTION", "cumulative_amount_yuan": 100},
    ]
    summary = summarize_curve(rows)
    assert summary["t50_timestamp"].endswith("09:24:00")
    assert summary["last_60s_share_pct"] == 40.0
    assert "LATE_RUSH" in summary["labels"]


def test_html_contains_market_and_plate_small_multiples():
    ticks = []
    for i in range(5):
        ticks.append(Tick("2026-10-14", "09:24:00", f"60000{i}", pc_milli=10000,
                          bp1_milli=10000, ap1_milli=10000, bv1=10, av1=10))
    report = build_bar_report(trade_date="2026-10-14", auction_ticks=ticks,
        relations={"p": {f"60000{i}" for i in range(5)}}, plate_names={"p": "测试板块"})
    assert "大盘额曲线" in report["html"]
    assert "测试板块" in report["html"]
    assert "<svg" in report["html"]
