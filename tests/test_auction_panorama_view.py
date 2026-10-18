from engine_core.auction_panorama_view import render_auction_email_digest, render_auction_panorama


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
        "quickbrief": {"lines": ["09:26 竞价速览 | 测试", "市场普遍高开"]},
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
    assert "09:26 竞价速览 | 测试" in html + plain
    assert "市场普遍高开" in html + plain
    assert "<details" not in html and "000001" not in html
    assert "基线样本14/64（样本不足，仅参考）" in plain
    assert "0.98倍 / 42.9分位" not in plain
    assert "规则一句话：" in plain
    assert "昨日涨停高开率55.6%低于市场90.2%" in plain
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
    assert "预览覆盖0/10" not in plain
    assert "覆盖分母为本页选中板块" in plain
    assert "未收录于热榜计为 —" in html + plain
    assert "封单金额/名次待核实" not in plain
    assert "1.20亿" in plain and "1.23亿" in plain
    assert "Top1金额占比" not in plain
    assert "选择：强/金额Top3" in plain


def test_seal_summary_scopes_are_explicit_and_hot_coverage_is_single_source():
    product = {"overview": {"verified_seal_sum_yuan": 4_686_000_000,
        "verified_seal_scope": "7/7", "yesterday_pool_verified_seal_n": 3,
        "yesterday_pool_verified_seal_sum_yuan": 1_424_000_000,
        "yesterday_pool_market_share": .304}, "plates": [
            {"plate_name": "甲", "hot_rank": 1, "stocks": []},
            {"plate_name": "乙", "hot_rank": None, "stocks": []}],
        "yesterday_ladder": []}
    _html, plain = render_auction_panorama(product)
    assert "竞价涨停封单合计：46.86亿（核实 7/7" in plain
    assert "全市场已核实封单 7 只、合计 46.86亿" in plain
    assert "昨日涨停池内已核实封单 3 只、合计 14.24亿" in plain
    assert "占全市场 30.4%" in plain
    assert "热榜覆盖 1/2" in plain
    assert "预览覆盖0/10" not in plain
    rule = plain.split("规则一句话：", 1)[1].splitlines()[0]
    assert "封单合计" not in rule
    assert "（（" not in rule


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
    product = {"overview": {"yesterday_pool_auction_amount_yuan": 100000000,
        "yesterday_pool_market_share": .2, "yesterday_pool_open_counts": {"high": 1, "flat": 1, "low": 1},
        "yesterday_pool_open_valid_n": 3, "multi_board_mean_auction_amount_yuan": 20000000,
        "first_board_mean_auction_amount_yuan": 10000000,
        "multi_to_first_board_mean_multiple": 2, "yesterday_pool_worst_two": [
            {"symbol": "000001", "name": "最弱股", "pct_change": -5}],
        "yesterday_pool_verified_seal_n": 2, "yesterday_pool_verified_seal_sum_yuan": 300000000},
        "plates": [], "yesterday_ladder": [{"yesterday_streak": 1, "count": 1,
        "buckets": [{"reason": "原因", "count": 3, "provisional_promotion_count": 1,
            "unverified_promotion_count": 1, "not_promoted_count": 1, "stocks": [
                {"symbol": "000001", "promotion_status": "UNVERIFIED", "pct_change": 10.0,
                 "primary_reason": "原因未核实", "related_theme": "冰雪产业"},
                {"symbol": "000002", "promotion_status": "NOT_PROMOTED", "pct_change": 8.3},
                {"symbol": "000003", "promotion_status": "PROVISIONAL", "pct_change": 8.3,
                 "at_official_limit": True}]}]}]}
    html, plain = render_auction_panorama(product)
    assert "待核实" in html + plain
    assert "未晋级迹象" in html + plain
    assert "封单金额/名次待核实" in html + plain
    assert "原因未核实" in html + plain and "关联板块：冰雪产业" in html + plain
    assert "昨日涨停池竞价额 1.00亿" in plain
    assert "高/平/低开 1/1/1" in plain and "未控制流动性" in plain
    assert "最弱股 -5.00%" in plain and "昨日涨停池内已核实封单 2 只、合计 3.00亿" in plain


def test_c302_digest_two_tables_six_blocks_and_only_authorized_stock_names():
    product = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
        "overview": {"auction_amount_yi": 10, "up_ratio": {"ratio": .5},
            "market_median_pct": 0, "yesterday_pool_n": 4, "yesterday_pool_open_valid_n": 4,
            "yesterday_pool_open_counts": {"high": 2, "flat": 1, "low": 1},
            "yesterday_pool_high_open_gap_pp": 5, "yesterday_pool_market_share": .2,
            "yesterday_pool_median_pct": .8, "yesterday_limit_down": {"status": "UNVERIFIED"},
            "extreme_threshold_pp_v1": 3.0,
            "relay_thresholds_v1": {"min_n": 3, "strong_pp": 15, "weak_pp": -15},
            "amount_multiple_64d": {"sample_n": 64, "window_n": 64},
            "market_extremes": {"highest": {"name": "极值股", "symbol": "000001", "pct_change": 5,
                "auction_amount_yuan": 100000, "amount_rank": 1, "limit_mark": "涨停封"},
                "lowest": None, "verified_seal_max": {"name": "封单股", "symbol": "000002",
                    "seal_amount_yuan": 300000, "seal_rank": 1, "amount_rank": 2}},
            "yesterday_pool_high_boards": [{"name": "高标股", "yesterday_streak": 3,
                "primary_reason": "题材", "pct_change": 2, "auction_amount_yuan": 200000,
                "amount_rank": 2}],
            "yesterday_reason_buckets": [{"reason": "题材", "count": 3, "highest_streak": 3,
                "open_counts": {"high": 2, "flat": 0, "low": 1}, "median_pct": 1,
                "provisional_n": 1, "not_promoted_n": 1, "unverified_n": 1,
                "willingness": "分歧", "auction_amount_yuan": 200000,
                "market_amount_share": .02, "extremes": {"strongest": None, "weakest": None}}]},
        "plates": [{"plate_id": "P", "plate_name": "题材", "selection_reasons": ["强"],
            "count": 3, "valid_price_count": 3, "amount_yuan": 200000, "market_share": .02,
            "member_median_pct": 1, "up_count": 2, "up_ratio": 2/3,
            "distribution": {"ge_3_n": 0, "zero_to_3_n": 2, "lt_0_n": 1, "valid_n": 3, "majority": "0~+3%"},
            "dispersion_p75_p25_pp": 1.5, "extremes": {"strongest": None, "weakest": None},
            "yesterday_relay": {"n": 3, "open_counts": {"high": 2, "flat": 0, "low": 1},
                "provisional_n": 1, "not_promoted_n": 1, "unverified_n": 1, "willingness": "分歧"},
            "verified_seal_n": 1, "verified_seal_amount_yuan": 300000,
            "seal_extreme": {"name": "封单股", "seal_amount_yuan": 300000, "seal_rank": 1, "amount_rank": 2},
            "stocks": [{"name": "不应出现股", "symbol": "000003"}]}]}
    quickbrief = {"email_lines": ["结论:题材接力意愿分歧；高标有拟晋级迹象"],
                  "metrics": {"market": {"auction_amount_yuan": 1_000_000_000}}}
    html, plain = render_auction_email_digest(product, quickbrief, completeness="READY")
    assert html.count('role="table"') == 2
    assert "昨日整批统计" in html and "昨日跌停:待核实" in html
    assert "题材横向对比" in html and "昨日涨停×题材 接力意愿" in html
    assert all(name in html for name in ("极值股", "封单股", "高标股"))
    assert "不应出现股" not in html
    assert "明显极值：距题材中位≥3.00pp" in html
    assert "明显分化：P75−P25≥4.0pp" in html
    assert "偏弱=高开率低于市场≥15pp" in html
    assert "离散度—" in plain
    assert len(html.encode()) <= 30_000
    assert "<script" not in html.lower() and "https://" not in html.lower()
    assert len(plain.split("\n\n")) == 5
    assert "<span" not in plain and "</span>" not in plain
    assert "分档≥+3%" in plain
    assert html.count("高标股 · 昨3板") == 1
    assert "高标股 · 昨3板" in html
    assert html.index("① 题材横向对比") < html.index("② 昨日涨停×题材 接力意愿") < html.index("高标与封单极值")
