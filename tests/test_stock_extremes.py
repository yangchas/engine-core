from engine_core import derive_stock_extremes
from engine_core.facts import FactStatus
from engine_core.market_auction_report import build_market_auction_report
from engine_core.q2 import normalize_q2


def _q(symbol, *, px=1000, pc=1000, am=0, br=0, ar=0, a25=0, ls=0, name="n"):
    return normalize_q2(symbol, {
        "px": str(px), "pc": str(pc), "amt": "0", "vol": "0", "ts": "1760000000000", "ph": "0",
        "am": str(am), "br": str(br), "ar": str(ar), "a25": str(a25), "ls": str(ls), "a20": "0", "a24": "0",
    })


def _cohort():
    return {
        "000001": _q("000001", am=500, a25=1100, pc=1000),            # +1000 bp
        "000002": _q("000002", am=900, a25=950, pc=1000),             # -500 bp
        "000003": _q("000003", am=300, a25=1000, pc=1000, br=70, ls=1),  # 0 bp, limit up seal 70
        "000004": _q("000004", am=100, a25=0, pc=1000),               # anchor missing
        "000005": _q("000005", am=0, br=0, ar=0, a25=0),              # not a candidate
    }


def test_rankings_are_single_measure_ordered_with_stable_ties():
    out = derive_stock_extremes(_cohort(), trade_date="2026-10-13", top_n=2)
    assert out.candidate_count == 4
    assert [r["symbol"] for r in out.rankings["auction_amount_top"]] == ["000002", "000001"]
    assert [(r["symbol"], r["value"]) for r in out.rankings["auction_gain_top"]] == [("000001", 1000), ("000003", 0)]
    assert [(r["symbol"], r["value"]) for r in out.rankings["auction_drop_top"]] == [("000002", -500), ("000003", 0)]
    assert [(r["symbol"], r["value"]) for r in out.rankings["limit_up_seal_top"]] == [("000003", 70)]
    assert out.units["auction_gain_top"] == "basis_points"


def test_missing_measure_is_excluded_not_ranked_as_zero_and_marks_partial():
    out = derive_stock_extremes(_cohort(), trade_date="2026-10-13", top_n=10)
    assert out.excluded_counts["auction_gain_top"] == 1  # 000004 has no a25
    assert "000004" not in {r["symbol"] for r in out.rankings["auction_gain_top"]}
    assert out.status is FactStatus.PARTIAL


def test_recovered_anchor_enters_the_gain_ranking():
    out = derive_stock_extremes(
        _cohort(), trade_date="2026-10-13", top_n=10, recovered_anchor_milli={"000004": 1200})
    assert out.rankings["auction_gain_top"][0] == {
        "rank": 1, "symbol": "000004", "name": None, "value": 2000} or \
        out.rankings["auction_gain_top"][0]["symbol"] == "000004"
    assert out.excluded_counts["auction_gain_top"] == 0


def test_amount_delta_only_when_supplied_and_unknown_delta_excluded():
    base = derive_stock_extremes(_cohort(), trade_date="2026-10-13")
    assert "amount_delta_top" not in base.rankings and base.notes
    with_delta = derive_stock_extremes(
        _cohort(), trade_date="2026-10-13", top_n=3,
        amount_delta_by_symbol={"000001": 400.0, "000002": None, "000003": -50.0})
    assert [r["symbol"] for r in with_delta.rankings["amount_delta_top"]] == ["000001", "000003"]
    assert with_delta.excluded_counts["amount_delta_top"] == 2  # 000002 None, 000004 absent


def test_empty_cohort_is_unavailable_and_hash_is_deterministic():
    assert derive_stock_extremes({}, trade_date="2026-10-13").status is FactStatus.UNAVAILABLE
    a = derive_stock_extremes(_cohort(), trade_date="2026-10-13")
    b = derive_stock_extremes(_cohort(), trade_date="2026-10-13")
    assert a.content_hash == b.content_hash


def test_report_renders_extremes_without_scores_or_conclusions():
    ex = derive_stock_extremes(_cohort(), trade_date="2026-10-13", top_n=2)
    report = build_market_auction_report(
        trade_date="2026-10-13", event_id="E", data_origin="T", stock_extremes=ex)
    assert "个股竞价极值" in report.text and "无评分、无结论" in report.text
    assert "竞价涨幅(bp)" in report.text and "排除缺值 1" in report.text
    assert report.component_status["stock_extremes"] == "PARTIAL"
