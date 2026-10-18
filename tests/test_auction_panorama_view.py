from engine_core.auction_panorama_view import render_auction_panorama


def test_panorama_view_renders_two_complete_tables_and_preserves_unknowns():
    panorama = {
        "schema": "auction_panorama_v1", "trade_date": "2026-10-16",
        "asof": "2026-10-16T09:26:00+08:00",
        "overview": {
            "auction_amount_yi": 137.889, "amount_multiple_64d": {"value": 0.9752,
                "percentile": 0.4286, "sample_n": 14, "window_n": 64,
                "basis": "REPLAY_EVENT_TIME", "status": "PARTIAL"},
            "up_ratio": {"numerator": 4617, "denominator": 5119, "ratio": 0.902},
            "market_median_pct": 1.2,
            "yesterday_limit_high_open": {"numerator": 20, "denominator": 36},
            "yesterday_vs_market_pp": -34.6378, "verified_seal_sum_yuan": 4685868597,
            "verified_seal_scope": "7/7", "verified_seal_label": "回放·按事件时间",
            "market_structure": "有效价格上涨4617/5119；中位涨幅1.20%"},
        "plates": [{"plate_name": "示例板块", "member_count": 5, "small_count": 5,
            "medium_count": 0, "large_count": 0, "amount_yuan": 1000,
            "amount_share": 0.5, "equal_weight_pct": 2.0, "up_count": 3,
            "valid_price_n": 5, "top1_share": 0.7, "hot_rank": None,
            "stocks": [{"symbol": "000001", "name": "示例股", "pct_change": 2.0,
                "auction_amount_yuan": 1000, "amount_rank": 1, "amount_top50": True,
                "yesterday_streak": 2, "seal_amount_yuan": None, "seal_rank": "UNKNOWN",
                "extremes": ["额Top50"]}]}],
        "yesterday_ladder": [{"yesterday_streak": 2, "count": 1, "buckets": [{
            "reason": "未知", "count": 1, "auction_amount_yuan": 1000,
            "layer_amount_share": 1.0, "high_open_ratio": 1.0, "low_open_ratio": 0.0,
            "median_pct": 2.0, "verified_seal_amount_yuan": None,
            "stocks": [{"symbol": "000001", "name": "示例股", "pct_change": 2.0,
                "auction_amount_yuan": 1000, "amount_rank": 1, "amount_top50": True,
                "seal_amount_yuan": None, "seal_rank": "UNKNOWN", "primary_reason": "未知",
                "promotion_status": "PROVISIONAL", "extremes": []}]}]}],
        "extremes": {"footer": "最大低开 000001 -2.00%"},
        "lineage": {"hot_plate_ranks": "PROVIDED", "hot_rank_basis": "POSTHOC_DEV"},
    }
    html, plain = render_auction_panorama(panorama)
    assert html.count('role="table"') == 2
    assert "额Top50" in html and "额Top50" in plain
    assert "回放·按事件时间" in html and "回放·按事件时间" in plain
    assert "封单金额/名次待核实" not in html + plain
    assert "开盘啦热板名次：—" in html + plain
    assert "POSTHOC_DEV" in html + plain
    assert "收盘后回溯，非09:26快照，仅供开发评估" in html + plain
    assert "金额待核实" not in html + plain
    assert "<details" not in html and "000001" not in html
    assert "基线样本14/64（样本不足，仅参考）" in plain
    assert "0.98倍 / 42.9分位" not in plain
    assert "规则一句话：" in plain
    assert "（均按竞价价>昨收）" in plain
    assert "市场结构" not in plain
    assert "成员涨幅中位数" in plain
    assert "本层占比" not in plain


def test_posthoc_hot_coverage_and_readable_amounts_and_selection_reasons():
    product = {"lineage": {"hot_rank_basis": "POSTHOC_DEV"}, "plates": [
        {"plate_name": "强势板块", "selection_reasons": ["强", "金额Top3"],
         "amount_yuan": 123456789, "stocks": [
             {"name": "普通股", "pct_change": 1, "auction_amount_yuan": 120000000,
              "amount_rank": 1, "seal_amount_yuan": None, "seal_rank": "UNKNOWN",
              "extremes": ["Top1金额占比"]},
             {"name": "涨停股", "pct_change": 10, "auction_amount_yuan": 250000,
              "amount_rank": 10, "seal_amount_yuan": 12345678, "seal_rank": 2,
              "extremes": []}]}], "yesterday_ladder": []}
    html, plain = render_auction_panorama(product)
    assert "收盘后回溯，非09:26快照，仅供开发评估" in plain
    assert "热榜覆盖 0/1" in plain
    assert "开盘啦热榜口径=热板Top50；258行版本为更宽的列表" in plain
    assert "未收录于热榜计为 —" in html + plain
    assert "封单金额/名次待核实" not in plain
    assert "1.20亿" in plain and "1.23亿" in plain
    assert "Top1金额占比" not in plain
    assert "选择：强/金额Top3" in plain


def test_ready_baseline_keeps_multiple_and_partial_baseline_is_gray():
    product = {"trade_date": "2026-10-16", "overview": {
        "auction_amount_yi": 10,
        "amount_multiple_64d": {"value": 1.25, "percentile": .5, "sample_n": 64,
            "window_n": 64, "status": "READY"}}, "plates": [], "yesterday_ladder": []}
    _html, ready = render_auction_panorama(product)
    assert "1.25倍 / 50.0分位" in ready
    product["overview"]["amount_multiple_64d"].update({"sample_n": 8, "status": "PARTIAL"})
    html, partial = render_auction_panorama(product)
    assert "1.25倍 / 50.0分位" not in partial
    assert "基线样本8/64（样本不足，仅参考）" in partial
    assert "color:#98a2b3" in html


def test_unverified_promotion_is_displayed_as待核实():
    product = {"plates": [], "yesterday_ladder": [{"yesterday_streak": 1, "count": 1,
        "buckets": [{"reason": "原因", "count": 1, "provisional_promotion_count": 0,
            "unverified_promotion_count": 1, "stocks": [{"symbol": "000001",
                "promotion_status": "UNVERIFIED", "pct_change": 10.0}]}]}]}
    html, plain = render_auction_panorama(product)
    assert "待核实" in html + plain
    assert "未晋级迹象" not in html + plain
