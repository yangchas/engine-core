"""C577: ladder_daily pure builder on REAL captured daily_kline / Kaipan pool rows (tests/fixtures/ladder_daily)."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from ladder_support import by_symbol, fixture, request

from engine_core.data_pipeline import ladder_daily as ld
from engine_core.data_pipeline.derived_daily import UpstreamNotReady
from engine_core.data_pipeline.ladder_daily import NeedInput, board_of, build_ladder_daily, limit_price


def build(**kw):
    return build_ladder_daily(request(**kw))


# ----------------------------------------------------------------------------------------------- the C574 incident
def test_incident_symbols_are_right_on_1027():
    rows = by_symbol(build())
    assert rows["002724"]["streak_n"] == 1                 # 10-26 -3.49% broke the 6-board the old relay carried
    assert rows["600838"]["streak_n"] == 5                 # 10-21..10-27 consecutive limit-ups
    assert rows["600838"]["streak_start_date"] == "2026-10-21"
    assert all(r["quality"] == "OK" for r in rows.values())


def test_independent_streaks_from_raw_pct_chg():
    """Independent recomputation: pct_chg thresholds per prefix straight from the bars (no pre_close rounding,
    no builder code), compared symbol by symbol for 10-27."""
    fx = fixture()
    thr = lambda s: {"688": 19.9, "689": 19.9, "300": 19.9, "301": 19.9, "302": 19.9}.get(s[:3], 9.9)
    up = lambda d, s: next((thr(s) <= (r.get("pct_chg") or 0) for r in fx["kline"][d]["rows"] if r["symbol"] == s), False)
    got = by_symbol(build())
    days = fx["days"]
    bad = []
    for s in {r["symbol"] for r in fx["kline"]["2026-10-27"]["rows"]}:
        if s[:2] in ("43", "83", "87", "88", "92"):
            continue
        n, i = 0, len(days) - 1
        while i >= 1 and up(days[i], s):
            n, i = n + 1, i - 1
        if (got[s]["streak_n"] if s in got else 0) != n:
            bad.append((s, n, got.get(s, {}).get("streak_n")))
    assert len(bad) <= 3, bad       # rounding edge cases of a 0.1 pct tolerance only


def test_kaipan_pool_agrees_on_streak_depth():
    fx = fixture()
    got = by_symbol(build())
    lb = {r["symbol"]: r["lb_days"] for r in fx["pools"]["2026-10-27"]["rows"]}
    diff = [(s, n, got[s]["streak_n"]) for s, n in lb.items() if s in got and got[s]["streak_n"] != n]
    assert len(diff) <= 3, diff


def test_cross_validation_is_recorded_not_silent():
    part = build()
    cv = part.manifest["quality"]["cross_validation"]
    assert cv["2026-10-27"]["kline_count"] == 44 and cv["2026-10-27"]["kaipan_count"] == 41
    assert cv["2026-10-27"]["kline_only"] == 3 and cv["2026-10-27"]["kline_only_examples"]


# ----------------------------------------------------------------------------------------------- source ladder
def test_missing_kline_day_falls_back_to_kaipan_for_that_day_only():
    part = build(drop_kline=["2026-10-22"])
    m = part.manifest
    chain = by_symbol(part)["600838"]["day_source_chain"].split(">")
    assert chain[:2] == ["derived_daily_kline"] * 2        # 27, 26 from bars
    assert chain[2:4] == ["kaipan", "kaipan"]              # 23 (previous bar missing -> not provably continuous), 22
    assert m["provider"] == "derived_daily_kline" and m["provider_chain"][:2] == ["derived_daily_kline", "kaipan"]
    reasons = [a["reason"] for a in m["provider_attempts"] if a["result"] == "unusable"]
    assert any("2026-10-22:daily_kline_missing" in r for r in reasons)
    assert any("2026-10-23:previous_day_kline_missing" in r for r in reasons)   # continuity rule, next day too


def test_continuity_rule_not_just_presence():
    """10-26 present but 10-23 absent -> 10-26 bars cannot be proven continuous -> that day uses Kaipan."""
    part = build(drop_kline=["2026-10-23"])
    attempts = [a for a in part.manifest["provider_attempts"] if a["result"] == "unusable"]
    assert any("2026-10-26:previous_day_kline_missing" in a["reason"] for a in attempts)
    day26 = part.manifest["inputs"]["days"]["2026-10-26"]
    assert day26["source"] == "kaipan"


def test_discontinuous_bars_are_rejected_by_pre_close_check():
    fx = fixture()
    shifted = request()
    for r in shifted["days"]["2026-10-26"]["daily_kline"]["rows"]:
        r["pre_close"] = (r["pre_close"] or 0) * 1.5          # pre_close no longer equals the previous close
    part = build_ladder_daily(shifted)
    assert part.manifest["inputs"]["days"]["2026-10-26"]["source"] == "kaipan"
    assert any("not_continuous" in a["reason"] for a in part.manifest["provider_attempts"])


def test_wencai_rung_needs_date_evidence_then_unknown_truncates_chain():
    wencai_rows = [{"symbol": "600838"}, {"symbol": "000678"}]
    ok = {"status": "ok", "rows": wencai_rows, "date_evidence": "2026-10-22", "version": None}
    part = build(drop_kline=["2026-10-22"], drop_pool=["2026-10-22"], extra_days={"2026-10-22": {"wencai": ok}})
    assert part.manifest["inputs"]["days"]["2026-10-22"]["source"] == "wencai"
    wrong = {**ok, "date_evidence": "2026-10-23"}
    part = build(drop_kline=["2026-10-22"], drop_pool=["2026-10-22"], extra_days={"2026-10-22": {"wencai": wrong}})
    assert any("wencai_date_evidence_mismatch" in a["reason"] for a in part.manifest["provider_attempts"])
    # nothing answers 10-22 -> UNKNOWN: streaks through it are LOWER BOUNDS, never invented numbers
    row = by_symbol(part)["600838"]
    assert row["streak_n"] == 3 and row["quality"] == "LOWER_BOUND:unknown_day:2026-10-22"
    q = part.manifest["quality"]
    assert q["partial"] is True and q["chain_truncated_days"] == ["2026-10-22"]
    assert part.manifest["business_status"] == "DEGRADED" and part.manifest["degraded"] is True


def test_target_day_unknown_is_not_applicable():
    with pytest.raises(UpstreamNotReady) as exc:
        build(day="2026-10-27", drop_kline=["2026-10-27"], drop_pool=["2026-10-27"])
    assert exc.value.code == "LIMIT_UP_SET_UNKNOWN"


def test_ladder_is_configuration_driven():
    # the 10-27 Kaipan pool was captured 10-28 08:31: it is invisible at 23:00 on 10-27 (PIT), visible a day later
    assert build(source_ladder=["kaipan", "daily_kline"]).manifest["provider"] == "derived_daily_kline"
    part = build(source_ladder=["kaipan", "daily_kline"], as_of="2026-10-29T00:00:00+08:00")
    assert part.manifest["provider"] == "kaipan" and part.manifest["degraded"] is True
    with pytest.raises(UpstreamNotReady):       # only bars configured, bars of D dropped -> no other rung is tried
        build(source_ladder=["daily_kline"], drop_kline=["2026-10-27"])


def test_needs_input_when_not_loaded():
    req = request()
    del req["days"]["2026-10-22"]["daily_kline"]
    with pytest.raises(NeedInput) as exc:
        build_ladder_daily(req)
    assert (exc.value.date, exc.value.rung) == ("2026-10-22", "daily_kline")


# ----------------------------------------------------------------------------------------------- rules
def test_board_and_limit_rules():
    assert [board_of(s) for s in ("600000", "000001", "002724", "300750", "301314", "688981", "920207", "430047", "900901",
                                  "abc123")] == ["MAIN", "MAIN", "MAIN", "GEM", "GEM", "STAR", "BSE", "BSE", "UNKNOWN", "UNKNOWN"]
    assert limit_price("10.00", "MAIN") == Decimal("11.00")
    assert limit_price("9.95", "MAIN") == Decimal("10.95")          # 10.945 rounds half up to the cent
    assert limit_price("10.00", "GEM") == Decimal("12.00") and limit_price("10.00", "STAR") == Decimal("12.00")
    assert limit_price("10.00", "BSE") == Decimal("13.00")
    assert limit_price("10.00", "UNKNOWN") is None
    assert ld.BOARD_LIMIT_PCT == {"MAIN": 10, "GEM": 20, "STAR": 20, "BSE": 30}     # no ST 5% anywhere


def test_unknown_board_is_counted_not_defaulted_to_main():
    req = request()
    row = dict(next(r for r in req["days"]["2026-10-27"]["daily_kline"]["rows"] if r["symbol"] == "600838"))
    row["symbol"] = "900901"                                  # a B-share style code: board cannot be decided
    req["days"]["2026-10-27"]["daily_kline"]["rows"].append(row)
    part = build_ladder_daily(req)
    assert "900901" not in by_symbol(part)
    assert part.manifest["quality"]["day_stats"]["2026-10-27"]["board_unknown"] == 1


def test_new_listing_first_days_are_exempt_not_limit_ups():
    """600838 pretends to be a first-day listing: absent on 10-26 and 10-23 bars -> no price limit -> not a limit-up."""
    req = request()
    for d in ("2026-10-26", "2026-10-23", "2026-10-22"):
        req["days"][d]["daily_kline"]["rows"] = [r for r in req["days"][d]["daily_kline"]["rows"] if r["symbol"] != "600838"]
    part = build_ladder_daily(req)
    assert "600838" not in by_symbol(part)
    assert part.manifest["quality"]["day_stats"]["2026-10-27"]["exempt_new_listing"] >= 1


def test_listing_date_from_stock_static_wins():
    req = request(listings={"600838": "2026-10-26"})        # listing day 10-26 -> 10-27 is day 2 (still free, main <=5)
    part = build_ladder_daily(req)
    assert "600838" not in by_symbol(part)


def test_point_in_time_future_inputs_are_invisible():
    req = request(as_of="2026-10-27T10:00:00+08:00")        # 10-27 bars were available at 18:26 -> not visible yet
    with pytest.raises(UpstreamNotReady):
        build_ladder_daily(req | {"source_ladder": ["daily_kline"]})


def test_window_cap_marks_lower_bound():
    part = build(window={"max_days": 3})
    row = by_symbol(part)["600838"]
    assert row["streak_n"] == 3 and row["quality"] == "LOWER_BOUND:window_cap"
    assert part.manifest["quality"]["window_capped"] is True


# ----------------------------------------------------------------------------------------------- contract-ish
def test_deterministic_and_stateless():
    a, b = build(), build()
    assert json.dumps(a.rows, sort_keys=True) == json.dumps(b.rows, sort_keys=True)
    assert json.dumps(a.manifest, sort_keys=True) == json.dumps(b.manifest, sort_keys=True)
    assert [r["symbol"] for r in a.rows] == sorted(r["symbol"] for r in a.rows)
    # the same result when the days after the window start are all that exist: no previous ladder partition input
    assert "previous_ladder" not in json.dumps(request())


def test_manifest_records_inputs_and_quality_inheritance():
    m = build().manifest
    assert m["business_status"] == "PROVISIONAL"            # 10-26 bars are PROVISIONAL and the streaks run through
    assert m["quality"]["history_non_formal_days"][0] == "2026-10-26"
    assert m["inputs"]["days"]["2026-10-27"]["business_status"] == "FORMAL"
    assert {"dataset": "daily_kline", "trade_date": "2026-10-26"}.items() <= next(
        r for r in m["input_refs"] if r["trade_date"] == "2026-10-26" and r["dataset"] == "daily_kline").items()
    assert m["window"]["fixed_days"][0] == "2026-10-27"
    req = request()
    req["days"]["2026-10-26"]["daily_kline"]["version"] = "sha256-0000000000000000"    # an older input changed
    other = build_ladder_daily(req).manifest
    assert other["inputs"] != m["inputs"]                    # identity (and so the catalog version) differs
    assert build().manifest["inputs"] == m["inputs"]
