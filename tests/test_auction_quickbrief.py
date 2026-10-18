from engine_core.auction_quickbrief import build_auction_quickbrief
from engine_core.auction_panorama import build_auction_panorama


def test_quickbrief_thresholds_and_replay_banner():
    facts = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00", "seal_label": "回放",
        "rows": [{"symbol": f"00000{i}", "name": f"股{i}", "valid_price": True, "price": 10,
                  "pct_change": 1.0 if i <= 4 else -1.0, "auction_amount_yuan": 100,
                  "seal": {"status": "VERIFIED_REPLAY", "seal_amount_yuan": 10 * i}}
                 for i in range(1, 6)]}
    result = build_auction_quickbrief(facts, plate_members=[], yesterday_pool=[], replay=True)
    assert result["threshold_version"] == "V1"
    assert result["metrics"]["market"]["up_ratio"] == 0.8
    assert result["rules"]["market_open"] is True
    assert "普遍高开" in result["lines"][1]
    assert "历史回放样本" in result["lines"][0]
    assert len(result["lines"]) == 8


def test_quickbrief_low_price_coverage_and_missing_prices_do_not_count():
    facts = {"trade_date": "2026-10-16", "rows": [
        {"symbol": f"{i:06d}", "valid_price": i <= 18, "price": 10 if i <= 18 else None,
         "pct_change": 2.0 if i <= 18 else None,
         "auction_amount_yuan": 10} for i in range(1, 21)]}
    result = build_auction_quickbrief(facts, plate_members=[], yesterday_pool=[])
    assert result["metrics"]["market"]["valid_n"] == 18
    assert result["metrics"]["market"]["coverage"] == 0.9
    assert result["rules"]["market_coverage_sufficient"] is False
    assert result["lines"][1] == "市场 覆盖不足,不下结论"
    assert "暂无观察候选" in result["lines"][-2]


def test_quickbrief_excludes_missing_zero_negative_and_nan_prices_everywhere():
    facts = {"trade_date": "2026-10-16", "rows": [
        {"symbol": "000001", "valid_price": True, "price": 10, "pct_change": 1,
         "auction_amount_yuan": 10},
        {"symbol": "000002", "valid_price": True, "price": None, "pct_change": 2,
         "auction_amount_yuan": 10},
        {"symbol": "000003", "valid_price": True, "price": 0, "pct_change": 3,
         "auction_amount_yuan": 10},
        {"symbol": "000004", "valid_price": True, "price": -1, "pct_change": 4,
         "auction_amount_yuan": 10},
        {"symbol": "000005", "valid_price": True, "price": float("nan"), "pct_change": 5,
         "auction_amount_yuan": 10},
    ]}
    members = [{"plate_id": "P", "symbol": f"00000{i}"} for i in range(1, 6)]
    pool = [{"symbol": f"00000{i}"} for i in range(1, 6)]
    result = build_auction_quickbrief(facts, plate_members=members, yesterday_pool=pool)
    assert result["metrics"]["market"]["valid_n"] == 1
    assert result["metrics"]["yesterday_pool"]["valid_n"] == 1
    fields = result["rules"]["plate_metric_fields"]
    assert result["rules"]["plate_metrics"]["P"][fields.index("price_coverage")] == 0.2
    assert result["rules"]["rejections"]["P"].count("PRICE_COVERAGE_BELOW_MIN") == 1


def test_quickbrief_relay_sample_minimum_boundary():
    facts = {"trade_date": "2026-10-16", "rows": [
        {"symbol": f"{i:06d}", "valid_price": True, "price": 10,
         "pct_change": 1.0 if i <= 80 else -1.0, "auction_amount_yuan": 10}
        for i in range(1, 101)]}
    pool = [{"symbol": f"{i:06d}"} for i in range(1, 21)]
    for row in facts["rows"][:19]:
        row["pct_change"] = -1.0
    result = build_auction_quickbrief(facts, plate_members=[], yesterday_pool=pool[:19])
    assert result["metrics"]["yesterday_pool"]["valid_n"] == 19
    assert "样本不足" in result["lines"][1]
    assert "不弱于阈值" not in result["lines"][1]
    result = build_auction_quickbrief(facts, plate_members=[], yesterday_pool=pool)
    assert result["metrics"]["yesterday_pool"]["valid_n"] == 20
    assert "接力" in result["lines"][1]


def test_quickbrief_lines_include_threshold_note():
    result = build_auction_quickbrief({"rows": []}, plate_members=[], yesterday_pool=[])
    assert result["lines"][-1] == "阈值为 V1 冻结工程阈值,非市场规律,未回测"


def _fixture_for_plates():
    rows = []
    for i in range(1, 301):
        pct = 2.0 if i <= 120 or 221 <= i <= 226 else (0.1 if i <= 220 else -1.0)
        rows.append({"symbol": f"{i:06d}", "name": f"股{i}", "valid_price": True, "price": 10,
                     "pct_change": pct,
                     "auction_amount_yuan": float(1000 - i), "amount_rank": i,
                     "seal": {"status": "UNKNOWN"}})
    # Four eligible plates: main A, backup B, overlapping C, and broad D.
    members = []
    for plate, symbols in {
        "A": range(1, 121), "Z": range(1, 121), "B": range(221, 227),
        "C": range(1, 7), "D": range(1, 301),
    }.items():
        members.extend({"plate_id": plate, "plate_name": plate, "symbol": f"{i:06d}"}
                       for i in symbols)
    return {"trade_date": "2026-10-16", "rows": rows}, members


def test_quickbrief_plate_sort_overlap_and_top3_market_ranks():
    facts, members = _fixture_for_plates()
    result = build_auction_quickbrief(
        facts, plate_members=members, yesterday_pool=[],
        plate_classes={"D": "BROAD"})
    assert result["rules"]["main"]["plate_id"] == "A"
    fields = result["rules"]["plate_metric_fields"]
    assert result["rules"]["plate_metrics"]["A"][fields.index("auction_amount_yuan")] == result["rules"]["plate_metrics"]["Z"][fields.index("auction_amount_yuan")]
    assert result["rules"]["backup"]["plate_id"] == "B"
    assert result["rules"]["main"]["top_amount_stocks"][0]["amount_rank"] == 1
    assert "金额Top" in result["lines"][2]
    assert "#1" in result["lines"][2]
    assert result["rules"]["backup"]["n"] == 6
    assert result["rules"]["backup"]["price_n"] == result["rules"]["backup"]["amount_n"] == 6
    assert result["rules"]["backup_overlap_rejections"][0]["plate_id"] == "C"


def test_quickbrief_exact_threshold_edges_and_insufficient_amount_coverage():
    facts, members = _fixture_for_plates()
    # Equality qualifies: 90% price/amount coverage, D=+0.50%, E=+5pp.
    facts["rows"] = [{"symbol": f"{i:06d}", "name": f"股{i}", "valid_price": True, "price": 10,
                      "pct_change": (0.5 if i <= 8 else -1.0 if i == 9 else
                                     0.5 if i <= 152 else -1.0),
                      "auction_amount_yuan": 100.0, "amount_rank": i}
                     for i in range(1, 181)]
    boundary = [{"plate_id": "EQ", "plate_name": "等于边界", "symbol": f"{i:06d}"}
                for i in (*range(1, 10), 181)]
    passed = build_auction_quickbrief(facts, plate_members=boundary, yesterday_pool=[])
    assert passed["rules"]["backup"]["plate_id"] == "EQ"
    # One more missing auction amount takes coverage below the threshold.
    facts["rows"][0]["auction_amount_yuan"] = None
    failed = build_auction_quickbrief(facts, plate_members=boundary, yesterday_pool=[])
    assert failed["rules"]["main"] is None
    failed_fields = failed["rules"]["plate_metric_fields"]
    assert failed["rules"]["plate_metrics"]["EQ"][failed_fields.index("amount_coverage")] == 0.8
    assert "AMOUNT_COVERAGE_BELOW_MIN" in failed["rules"]["rejections"]["EQ"]


def test_quickbrief_no_candidates_and_market_relay_sentence():
    facts = {"trade_date": "2026-10-16", "rows": [
        {"symbol": f"{i:06d}", "name": f"股{i}", "valid_price": True, "price": 10,
         "pct_change": 1.2, "auction_amount_yuan": 100, "amount_rank": i}
        for i in range(1, 101)]}
    pool = [{"symbol": f"{i:06d}", "lb_days": 2} for i in range(1, 37)]
    for row in facts["rows"][:16]:
        row["pct_change"] = -1.0
    result = build_auction_quickbrief(facts, plate_members=[], yesterday_pool=pool)
    assert result["rules"]["relay_lag"] is True
    assert "接力明显落后" in result["lines"][1]
    assert "昨板高开20/36" in result["lines"][1]
    assert "暂无观察候选" in result["lines"][-2]


def test_quickbrief_market_amount_share_boundary_refill_and_empty():
    facts = {"trade_date": "2026-10-16", "rows": [
        {"symbol": f"{i:06d}", "valid_price": True, "price": 10,
         "pct_change": 0.5 if i <= 8 or 11 <= i <= 18 or 20 <= i <= 147 else -1.0,
         "auction_amount_yuan": 10.0 if i <= 18 else 110.0}
        for i in range(1, 181)]}
    facts["asof"] = "2026-10-16T09:26:00+08:00"
    # Total market auction amount is 18,000: X and Y each meet exactly 0.5%.
    members = ([{"plate_id": plate, "symbol": f"{i:06d}"}
                for plate, start in (("X", 1), ("Y", 10)) for i in range(start, start + 9)])
    result = build_auction_quickbrief(facts, plate_members=members, yesterday_pool=[])
    v6 = build_auction_panorama(facts, plate_members=members, yesterday_pool=[])
    assert result["metrics"]["market"]["auction_amount_yuan"] == v6["overview"]["auction_amount_yuan"]
    assert result["metrics"]["market"]["auction_amount_yuan"] == 18000.0
    assert result["thresholds"]["min_plate_amount_share"] == 0.005
    assert result["rules"]["backup"]["plate_id"] == "X"
    assert result["rules"]["backup"]["market_amount_share"] == 0.005

    # C296 audit probe: negative amounts must not change V6's denominator.
    facts["rows"][-1]["auction_amount_yuan"] = -10000.0
    result = build_auction_quickbrief(facts, plate_members=members, yesterday_pool=[])
    v6 = build_auction_panorama(facts, plate_members=members, yesterday_pool=[])
    assert result["metrics"]["market"]["auction_amount_yuan"] == v6["overview"]["auction_amount_yuan"]
    assert result["metrics"]["market"]["auction_amount_yuan"] == 17890.0
    assert result["metrics"]["market"]["auction_amount_yuan"] == 17890.0
    facts["rows"][-1]["auction_amount_yuan"] = 110.0

    # X falls just below the boundary; Y must move into its place.
    facts["rows"][0]["auction_amount_yuan"] = 9.99
    result = build_auction_quickbrief(facts, plate_members=members, yesterday_pool=[])
    assert result["rules"]["backup"]["plate_id"] == "Y"
    assert "PLATE_AMOUNT_SHARE_BELOW_MIN" in result["rules"]["rejections"]["X"]
    assert result["rules"]["backup_top_scores"][0]["plate_id"] == "Y"
    assert result["rules"]["rejected_backup_scores"]["X"]["reason"] == "PLATE_AMOUNT_SHARE_BELOW_MIN"

    facts["rows"][9]["auction_amount_yuan"] = 9.99
    result = build_auction_quickbrief(facts, plate_members=members, yesterday_pool=[])
    assert result["rules"]["backup"] is None
    assert "暂无观察候选" in result["lines"][-2]

    # Exact C296 erroneous-admission probe: 499 / 100,000 is below 0.5%;
    # counting the -10,000 row would shrink the denominator to 90,000.
    audit_rows = [{"symbol": f"{i:06d}", "valid_price": True, "price": 10,
                   "pct_change": 4 if i <= 5 else 3 if i <= 10 else -1,
                   "auction_amount_yuan": None} for i in range(1, 1001)]
    for row in audit_rows[:5]:
        row["auction_amount_yuan"] = 499 / 5
    audit_rows[-1]["auction_amount_yuan"] = 100000 - 499
    audit_rows[-2]["auction_amount_yuan"] = -10000
    audit_facts = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
                   "rows": audit_rows}
    audit_members = [{"plate_id": "A", "symbol": f"{i:06d}"} for i in range(1, 6)]
    quick = build_auction_quickbrief(audit_facts, plate_members=audit_members, yesterday_pool=[])
    panorama = build_auction_panorama(audit_facts, plate_members=audit_members, yesterday_pool=[])
    assert panorama["overview"]["auction_amount_yuan"] == 100000.0
    assert quick["metrics"]["market"]["auction_amount_yuan"] == 100000.0
    assert quick["rules"]["backup"] is None
    assert "PLATE_AMOUNT_SHARE_BELOW_MIN" in quick["rules"]["rejections"]["A"]
