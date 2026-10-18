from engine_core.auction_panorama import build_auction_panorama, normalize_hot_plates


def test_hot_plate_rank_basis_ties_and_live_rejects_posthoc(monkeypatch):
    rows = [{"code": "B", "strength": 10, "change_pct": 2.5},
            {"code": "A", "strength": 10, "change_pct": -1.0},
            {"code": "C", "strength": 8, "change_pct": 1.0}]
    live = normalize_hot_plates(rows, fetched_at="2026-10-16T09:25:00+08:00",
        available_at="2026-10-16T09:25:00+08:00",
        asof="2026-10-16T09:26:00+08:00", trade_date="2026-10-16")
    assert live["basis"] == "PIT_LIVE"
    assert live["ranks"]["A"]["hot_rank"] == live["ranks"]["B"]["hot_rank"] == 1
    assert live["coverage"] == "3/3"
    posthoc = normalize_hot_plates(rows, fetched_at="2026-10-17T08:44:00+08:00",
        available_at="2026-10-17T08:44:00+08:00",
        asof="2026-10-16T09:26:00+08:00", trade_date="2026-10-16")
    assert posthoc["basis"] == "UNKNOWN"
    rejected = normalize_hot_plates(rows, fetched_at="2026-10-17T08:44:00+08:00",
        available_at="2026-10-17T08:44:00+08:00",
        asof="2026-10-16T09:26:00+08:00", trade_date="2026-10-16",
        now="2026-10-16T10:00:00+08:00")
    assert rejected["basis"] == "UNKNOWN" and rejected["ranks"] == {}
    monkeypatch.setenv("POSTHOC_DEV", "1")
    development = normalize_hot_plates(rows, fetched_at="2026-10-17T08:44:00+08:00",
        available_at="2026-10-17T08:44:00+08:00",
        asof="2026-10-16T09:26:00+08:00", trade_date="2026-10-16",
        now="2026-10-18T10:00:00+08:00")
    assert development["basis"] == "POSTHOC_DEV"
    assert development["coverage"] == "3/3"


def _facts():
    rows = [
        {"symbol": "000001", "name": "A", "auction_amount_yuan": 300, "amount_rank": 1,
         "amount_top50": True, "pct_change": 2.0, "valid_price": True,
         "seal": {"status": "VERIFIED_REPLAY", "seal_amount_yuan": 900, "seal_rank": 1}},
        {"symbol": "000002", "name": "B", "auction_amount_yuan": 200, "amount_rank": 2,
         "amount_top50": True, "pct_change": -1.0, "valid_price": True,
         "seal": {"status": "UNKNOWN", "seal_amount_yuan": None, "seal_rank": None}},
        {"symbol": "000003", "name": "C", "auction_amount_yuan": 100, "amount_rank": 3,
         "amount_top50": True, "pct_change": None, "valid_price": False,
         "seal": {"status": "VERIFIED_REPLAY", "seal_amount_yuan": 700, "seal_rank": 2}},
    ]
    return {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
            "seal_label": "回放·按事件时间", "summary": {"verified_seal_n": 2,
            "limit_candidate_n": 3, "verified_seal_sum": 1600}, "rows": rows}


def test_panorama_preserves_missing_values_and_global_ranks_across_plates():
    result = build_auction_panorama(
        _facts(),
        plate_members=[{"plate_id": "P1", "plate_name": "主题一", "symbol": "000001"},
                       {"plate_id": "P2", "plate_name": "主题二", "symbol": "000001"},
                       {"plate_id": "P2", "plate_name": "主题二", "symbol": "000002"}],
        yesterday_pool=[{"symbol": "000001", "name": "A", "lb_days": 2,
                         "primary_reason": "池原因"}],
        yesterday_reasons={"000001": ["历史原因", "静态原因"]},
        streak_feedback={"000001": "PROMOTED"},
        market_amount_history=[100, 200, 300],
    )
    assert result["schema"] == "auction_panorama_v1"
    assert result["overview"]["valid_price_n"] == 2
    assert result["overview"]["verified_seal_scope"] == "2/3"
    assert result["overview"]["amount_multiple_64d"]["status"] == "PARTIAL"
    assert result["plates"][0]["stocks"][0]["amount_rank"] == 1
    copies = [stock for plate in result["plates"] for stock in plate["stocks"]
              if stock["symbol"] == "000001"]
    assert len(copies) == 2
    assert {row["amount_rank"] for row in copies} == {1}
    assert {row["seal_rank"] for row in copies} == {1}
    assert result["yesterday_ladder"][0]["buckets"][0]["reason"] == "池原因"
    assert result["yesterday_ladder"][0]["buckets"][0]["stocks"][0]["promotion_status"] == "PROVISIONAL"


def test_panorama_caps_amount_history_to_latest_64_days():
    result = build_auction_panorama(_facts(), plate_members=[], yesterday_pool=[],
                                    market_amount_history=list(range(1, 66)))
    assert result["overview"]["amount_multiple_64d"]["sample_n"] == 64
    assert result["overview"]["amount_multiple_64d"]["status"] == "READY"


def test_panorama_lists_all_36_yesterday_limit_stocks_and_empty_reason_bucket():
    pool = [{"symbol": f"{i:06d}", "name": f"S{i}", "lb_days": 1} for i in range(1, 37)]
    result = build_auction_panorama(_facts(), plate_members=[], yesterday_pool=pool,
                                    market_amount_history=[])
    assert sum(len(bucket["stocks"]) for layer in result["yesterday_ladder"]
               for bucket in layer["buckets"]) == 36
    assert result["yesterday_ladder"][0]["buckets"][0]["reason"] == "UNKNOWN"
    assert result["overview"]["yesterday_limit_high_open"]["numerator"] == 1
    assert result["overview"]["yesterday_limit_high_open"]["denominator"] == 36


def test_panorama_uses_fixed_reason_precedence_and_handles_no_members_or_prices():
    result = build_auction_panorama(
        _facts(), plate_members=[],
        yesterday_pool=[{"symbol": "000001", "lb_days": 1, "static_reason": "静态"}],
        yesterday_reasons={"000001": ["旧因一", "旧因二"]},
        market_amount_history=[100],
    )
    stock = result["yesterday_ladder"][0]["buckets"][0]["stocks"][0]
    assert result["plates"] == []
    assert stock["primary_reason"] == "旧因一"
    assert result["overview"]["up_ratio"]["denominator"] == 2
    assert result["overview"]["market_median_pct"] == 0.5
    assert result["extremes"]["largest_gap_down"] == {"symbol": "000002", "pct_change": -1.0}


def test_pool_label_assignment_precedes_historical_and_static_reasons():
    result = build_auction_panorama(
        _facts(), plate_members=[],
        yesterday_pool=[{"symbol": "000001", "lb_days": 1}],
        yesterday_reasons={"000001": ["D-1历史原因"]},
        theme_assignments={"000001": {"source": "POOL_LABEL",
                                      "primary_theme": {"name": "昨日池标签"}}},
    )
    stock = result["yesterday_ladder"][0]["buckets"][0]["stocks"][0]
    assert stock["primary_reason"] == "昨日池标签"


def test_stock_name_map_fills_names_missing_from_auction_facts():
    facts = _facts()
    facts["rows"][0]["name"] = None
    result = build_auction_panorama(
        facts, plate_members=[{"plate_id": "P1", "plate_name": "主题一", "symbol": "000001"}],
        yesterday_pool=[{"symbol": "000001", "lb_days": 1}],
        name_map={"000001": "映射名称"},
    )
    assert result["plates"][0]["stocks"][0]["name"] == "映射名称"
    assert result["yesterday_ladder"][0]["buckets"][0]["stocks"][0]["name"] == "映射名称"
