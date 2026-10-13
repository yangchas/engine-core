from engine_core import reconstruct_auction_from_td
from engine_core.facts import FactStatus


def _r(symbol, tag, px=10000, bp=0, amt=100, bid=0, ask=0, ls=0, ts="2026-10-13 09:25:06"):
    return {"symbol": symbol, "auction_tag": tag, "px_milli": px, "chg_bp": bp,
            "match_amt_yuan": amt, "rest_bid_amt_yuan": bid, "rest_ask_amt_yuan": ask,
            "limit_state": ls, "trade_date": "20261013", "ts": ts}


def _rows():
    out = []
    for tag, amt in (("0920", 100), ("0924", 400), ("0925", 900)):
        out += [_r("000001", tag, bp=50, amt=amt, ts="2026-10-13 09:2%s:06" % tag[3]),
                _r("600000", tag, bp=-20, amt=amt * 2, ts="2026-10-13 09:2%s:06" % tag[3])]
    return out


def test_reconstructs_three_summaries_current_rows_and_theme_delta():
    out = reconstruct_auction_from_td("2026-10-13", _rows())
    assert out.status is FactStatus.READY
    assert out.row_counts == {"0920": 2, "0924": 2, "0925": 2}
    assert out.summaries["0925"]["auction_amount_yuan"] == 900 + 1800
    assert [r["symbol"] for r in out.current_0925.rows] == ["000001", "600000"]
    deltas = {r["symbol"]: r["amount_delta_yuan"] for r in out.theme_delta.rows}
    assert deltas == {"000001": 500, "600000": 1000}


def test_missing_tag_is_unavailable_not_zero_and_status_partial():
    rows = [r for r in _rows() if r["auction_tag"] != "0924"]
    out = reconstruct_auction_from_td("2026-10-13", rows)
    assert out.status is FactStatus.PARTIAL
    assert out.tag_status["0924"] == "UNAVAILABLE" and "0924" not in out.summaries
    assert out.theme_delta.rows == ()  # no 0924 snapshot: no delta, not zero deltas


def test_empty_input_is_unavailable_and_hash_is_deterministic():
    assert reconstruct_auction_from_td("2026-10-13", []).status is FactStatus.UNAVAILABLE
    a = reconstruct_auction_from_td("2026-10-13", _rows())
    b = reconstruct_auction_from_td("2026-10-13", list(reversed(_rows())))
    assert a.content_hash == b.content_hash


def test_rows_for_other_trade_date_are_excluded_and_make_result_partial():
    rows = _rows() + [_r("600001", "0925", amt=999, ts="2026-10-12 09:25:06")]
    rows[-1]["trade_date"] = "20261012"
    out = reconstruct_auction_from_td("2026-10-13", rows)
    assert out.status is FactStatus.PARTIAL
    assert out.row_counts == {"0920": 2, "0924": 2, "0925": 2}
    assert out.summaries["0925"]["auction_amount_yuan"] == 2700
    assert out.as_trace()["excluded_other_date_rows"] == 1


def test_unknown_limit_up_seal_makes_tag_and_reconstruction_partial():
    rows = [_r("000001", tag, amt=10, bid=None, ls=1) for tag in ("0920", "0924", "0925")]
    out = reconstruct_auction_from_td("2026-10-13", rows)
    assert out.tag_status["0925"] == "PARTIAL"
    assert out.status is FactStatus.PARTIAL
    assert out.summaries["0925"]["limit_up_seal_amount_yuan"] is None
