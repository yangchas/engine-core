from engine_core.plate_cross_table import build_plate_cross_table
from engine_core.email_board import _plates


def test_cross_table_keeps_member_amount_and_missing_yesterday_explicit():
    rows = build_plate_cross_table(
        states={
            "000001": {"pct_change": 2.0, "amount_yuan": 60, "limit_state": 1,
                       "lb_days": 2, "name": "甲", "seal_time": "09:31:05"},
            "000002": {"pct_change": -1.0, "amount_yuan": None, "limit_state": -1,
                       "name": "乙"},
        },
        relations={"801001": {"000001", "000002"}},
        plate_names={"801001": "测试板块"},
        market_amount_yuan=100,
        previous_rows=None,
        promotion_by_theme={"801001": {"promotion_rate": 0.5}},
    )
    row = rows[0]
    assert row["amount_yuan"] == 60
    assert row["amount_basis"] == "MEMBER_SUM_OVERLAP_REPEATED"
    assert row["amount_coverage"] == {"known": 1, "members": 2, "ratio": 0.5}
    assert row["market_share_pct"] == 60
    assert row["equal_weight_pct"] == 0.5
    assert (row["up_count"], row["down_count"]) == (1, 1)
    assert (row["limit_up_count"], row["limit_down_count"]) == (1, 1)
    assert row["highest_board"] == 2
    assert row["promotion_rate"] == 0.5
    assert row["yesterday_same_checkpoint"] == {
        "status": "UNKNOWN", "change_pp": None, "reason": "NO_PREVIOUS_SAME_CHECKPOINT_DATA"
    }
    assert row["leaders"]["amount_max"]["symbol"] == "000001"
    assert row["leaders"]["earliest_seal"]["symbol"] == "000001"


def test_cross_table_uses_code_for_previous_comparison_and_keeps_missing_amount_none():
    rows = build_plate_cross_table(
        states={"000001": {"pct_change": 1.0, "amount_yuan": None, "limit_state": None}},
        relations={"801827": {"000001"}},
        plate_names={"801827": "AI视频"},
        market_amount_yuan=None,
        previous_rows=[{"code": "801827", "equal_weight_pct": -2.0}],
    )
    row = rows[0]
    assert row["name"] == "AI视频"
    assert row["amount_yuan"] is None
    assert row["market_share_pct"] is None
    assert row["yesterday_same_checkpoint"]["change_pp"] == 3.0


def test_open_table_names_yesterday_same_checkpoint_separately_from_today_change():
    html, text = _plates({"chapters": {"plate_cross_table": {
        "checkpoint": "0945", "rows": [{
            "code": "801001", "name": "测试", "member_count": 10,
            "amount_yuan": 100_000_000, "amount_coverage": {"known": 9, "members": 10},
            "market_share_pct": 2.0, "equal_weight_pct": 1.0,
            "limit_up_count": 2, "limit_down_count": 1, "highest_board": 3,
            "promotion_rate": 0.5,
            "yesterday_same_checkpoint": {"status": "READY", "change_pp": 1.5},
            "leaders": {"amount_max": {"symbol": "000001", "name": "甲"}},
        }],
    }, "plate_linkage": {"opening_changes": [{
        "code": "801001", "name": "测试",
        "delta": {"up_share_pct": 2.0, "equal_weight_median_pct": 0.5,
                   "limit_up_count": 1, "limit_down_count": -1},
    }]}}}, "open")
    assert "板块横向比较 @0945" in html
    assert "较昨日同截点" in html
    assert "0925→0945 盘中变化" in html
    assert "1.50pp" in text
    assert "UNKNOWN/n=0" in text


def test_cross_table_splits_broad_and_marks_overlap_and_small_promotion_sample():
    rows = build_plate_cross_table(
        states={"000001": {"pct_change": 2, "amount_yuan": 10},
                "000002": {"pct_change": 1, "amount_yuan": 5},
                "000003": {"pct_change": 1, "amount_yuan": 5},
                "000004": {"pct_change": 1, "amount_yuan": 5},
                "000005": {"pct_change": 1, "amount_yuan": 5}},
        relations={"a": {"000001", "000002", "000003", "000004"},
                   "b": {"000001", "000002", "000003", "000004", "000005"},
                   "c": {"000001", "000002", "000003"}, "d": {"000005"}},
        plate_names={"a": "窄", "b": "重复", "c": "部分重叠", "d": "宽"},
        member_counts={"a": 4, "b": 5, "c": 3, "d": 400},
        promotion_by_theme={"a": {"promotion_rate": 1, "promoted": 1, "total": 2}},
    )
    a_row = next(row for row in rows if row["code"] == "a")
    assert a_row["class"] == "NICHE"
    assert a_row["overlap_with"] == [{"code": "b", "name": "重复", "jaccard": 0.8}]
    assert a_row["promotion_status"] == "样本少"
    assert a_row["promotion_numerator"] == 1
    assert a_row["promotion_denominator"] == 2
    assert rows[-1]["class"] == "BROAD"


def test_cross_table_reuses_member_sets_and_matches_brute_force_overlap():
    class CountingSet(set):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    relations = {
        f"p{i:03d}": CountingSet({f"{j:04d}" for j in range(i, i + 24)})
        for i in range(48)
    }
    rows = build_plate_cross_table(states={}, relations=relations)
    by_code = {row["code"]: row for row in rows}
    for code, members in relations.items():
        expected = []
        for other_code, other_members in relations.items():
            if code == other_code:
                continue
            union = members | other_members
            score = len(members & other_members) / len(union) if union else 0.0
            if score >= 0.8:
                expected.append({"code": other_code, "name": other_code, "jaccard": score})
        expected.sort(key=lambda item: (-item["jaccard"], item["code"]))
        assert by_code[code]["overlap_with"] == expected
    assert max(members.iterations for members in relations.values()) == 1


def test_cross_table_reports_point_in_time_historical_amount_multiple_percentile_and_n():
    rows = build_plate_cross_table(
        states={"000001": {"pct_change": 1, "amount_yuan": 500}},
        relations={"801001": {"000001"}},
        historical_by_code={"801001": {"median_yuan": 250, "percentile": 80, "sample_days": 6}},
    )
    row = rows[0]
    assert row["historical_amount_multiple"] == 2
    assert row["historical_amount_percentile"] == 80
    assert row["historical_amount_sample_days"] == 6
    assert row["historical_amount_status"] == "READY"


def test_cross_table_historical_unknown_and_small_sample_are_not_zero():
    rows = build_plate_cross_table(
        states={"000001": {"amount_yuan": 0}}, relations={"a": {"000001"}},
        historical_by_code={"a": {"median_yuan": 0, "percentile": 50, "sample_days": 2}},
    )
    assert rows[0]["historical_amount_multiple"] is None
    assert rows[0]["historical_amount_status"] == "样本少"
    no_history = build_plate_cross_table(states={}, relations={"a": {"000001"}})[0]
    assert no_history["historical_amount_status"] == "UNKNOWN"
    assert no_history["historical_amount_sample_days"] == 0
