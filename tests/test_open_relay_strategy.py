"""Unit tests for 09:45 open_relay_strategy (strict PIT, Main+GEM only, shadow ledger)."""
from __future__ import annotations

import json

from engine_core.open_relay_strategy import (
    build_open_relay_decision,
    evaluate_open_environment,
    is_tradable_board,
    record_shadow_ledger,
    select_anti_consensus_buckets,
    select_buy_point_c_stocks,
    settle_previous_shadow_ledger,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.zsets: dict[str, dict[str, int]] = {}

    def set(self, k: str, v: str, ex: int | None = None) -> None:
        self.kv[k] = v

    def get(self, k: str) -> str | None:
        return self.kv.get(k)

    def zadd(self, k: str, mapping: dict[str, int]) -> None:
        self.zsets.setdefault(k, {}).update(mapping)

    def zrevrange(self, k: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(k, {}).items(), key=lambda x: x[1], reverse=True)
        return [m for m, _ in items[start : end + 1]]


def test_tradable_board_excludes_star_and_bse():
    assert is_tradable_board("000001") is True
    assert is_tradable_board("600519") is True
    assert is_tradable_board("300750") is True
    assert is_tradable_board("301001") is True
    assert is_tradable_board("688322") is False
    assert is_tradable_board("689009") is False
    assert is_tradable_board("830799") is False
    assert is_tradable_board("920001") is False


def test_environment_regimes_and_missing_not_zero():
    # Missing ticks -> UNKNOWN degraded, never fabricates 0
    empty = evaluate_open_environment(
        ticks_0925={}, ticks_0945={}, prev_kline={},
        yest_streaks={}, yest_broken_syms=set(), yest_rotten_syms=set(),
        prior_5d_mkt_amt_0945=[], st_syms=set(),
    )
    assert empty["regime"] == "UNKNOWN"
    assert empty["max_total_weight_pct"] == 0

    # Deep green: big cap up (+2%), small cap down (-1%) -> yw < 0; vol5 >= 1.0
    ticks45 = {
        "600519": {"px_milli": 10200, "pc_milli": 10000, "amt_yuan": 6e8, "ap1_milli": 10210, "av1": 100, "bp1_milli": 10200},
        "002001": {"px_milli": 9900, "pc_milli": 10000, "amt_yuan": 5e8, "ap1_milli": 9910, "av1": 100, "bp1_milli": 9900},
    }
    prev_k = {
        "600519": {"amount_yuan": 1e10, "turnover_pct": 1.0},  # float cap 1000B
        "002001": {"amount_yuan": 1e8, "turnover_pct": 5.0},   # float cap 2B
    }
    deep_g = evaluate_open_environment(
        ticks_0925=ticks45, ticks_0945=ticks45, prev_kline=prev_k,
        yest_streaks={}, yest_broken_syms=set(), yest_rotten_syms=set(),
        prior_5d_mkt_amt_0945=[9e8, 9.5e8, 10e8, 9.8e8, 10e8], st_syms=set(),
    )
    assert deep_g["regime"] == "DEEP_GREEN"
    assert deep_g["yw_spread_pp"] < 0
    assert deep_g["vol_ratio_5d"] > 1.0
    assert deep_g["max_total_weight_pct"] == 80
    assert deep_g["max_single_weight_pct"] == 40

    # Red: small cap up (+2%), big cap down (-1%) -> yw > 0; vol5 < 1.0
    ticks45_red = {
        "600519": {"px_milli": 9900, "pc_milli": 10000, "amt_yuan": 3e8, "ap1_milli": 9910, "av1": 100, "bp1_milli": 9900},
        "002001": {"px_milli": 10200, "pc_milli": 10000, "amt_yuan": 3e8, "ap1_milli": 10210, "av1": 100, "bp1_milli": 10200},
    }
    red = evaluate_open_environment(
        ticks_0925=ticks45_red, ticks_0945=ticks45_red, prev_kline=prev_k,
        yest_streaks={}, yest_broken_syms=set(), yest_rotten_syms=set(),
        prior_5d_mkt_amt_0945=[9e8, 9.5e8, 10e8, 9.8e8, 10e8], st_syms=set(),
    )
    assert red["regime"] == "RED"
    assert red["max_total_weight_pct"] == 0


def test_buy_point_c_filters_star_st_sealed_and_sorts_by_prev_amount():
    top_buckets = [{"theme": "AI安全", "auc_rank": 15}]
    members = {"AI安全": {"688322", "002912", "002212", "300001", "000999", "600111"}}
    t25 = {s: {"amt_yuan": 5e6} for s in members["AI安全"]}
    t35 = {s: {"px_milli": 9900, "pc_milli": 10000, "amt_yuan": 3e7} for s in members["AI安全"]}
    t45 = {
        "688322": {"px_milli": 10050, "pc_milli": 10000, "amt_yuan": 2e8, "ap1_milli": 10060, "av1": 100},  # STAR -> excluded
        "002912": {"px_milli": 10050, "pc_milli": 10000, "amt_yuan": 2e8, "ap1_milli": 10060, "av1": 100},  # PASS (#1 by prev_amt)
        "002212": {"px_milli": 10100, "pc_milli": 10000, "amt_yuan": 1.5e8, "ap1_milli": 10110, "av1": 100},  # PASS (#2 by prev_amt)
        "300001": {"px_milli": 10300, "pc_milli": 10000, "amt_yuan": 2e8, "ap1_milli": 10310, "av1": 100},  # pct45 >= 2% -> excluded
        "000999": {"px_milli": 10050, "pc_milli": 10000, "amt_yuan": 2e8, "ap1_milli": 0, "av1": 0},        # sealed -> excluded
        "600111": {"px_milli": 10050, "pc_milli": 10000, "amt_yuan": 2e8, "ap1_milli": 10060, "av1": 100},  # ST -> excluded
    }
    prev_k = {
        "688322": {"pct_chg": -2.0, "amount_yuan": 20e8, "open": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.2},
        "002912": {"pct_chg": -1.5, "amount_yuan": 12e8, "open": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.15},
        "002212": {"pct_chg": -3.0, "amount_yuan": 8e8, "open": 10.3, "low": 9.9, "close": 10.0, "pre_close": 10.3},
        "300001": {"pct_chg": -2.0, "amount_yuan": 15e8, "open": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.2},
        "000999": {"pct_chg": -2.0, "amount_yuan": 15e8, "open": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.2},
        "600111": {"pct_chg": -2.0, "amount_yuan": 15e8, "open": 10.2, "low": 9.8, "close": 10.0, "pre_close": 10.2},
    }
    names = {"688322": "科创票", "002912": "中新赛克", "002212": "天融信", "300001": "特锐德", "000999": "封板票", "600111": "ST测试"}
    picks = select_buy_point_c_stocks(
        top_buckets=top_buckets, theme_members=members,
        ticks_0925=t25, ticks_0935=t35, ticks_0945=t45,
        prev_kline=prev_k, prev_dde={"002912": 2.5e7}, stock_names=names, st_syms=set(),
        max_stocks=2, max_single_weight_pct=40, trial_single_weight_pct=20, max_total_weight_pct=80,
    )
    assert [p["symbol"] for p in picks] == ["002912", "002212"]
    assert "★昨DDE吸筹" in picks[0]["tags"]
    assert "★竞价静默" in picks[0]["tags"]
    assert picks[0]["std_weight_pct"] == 40
    assert picks[0]["trial_weight_pct"] == 20
    # Ensure ZERO forbidden 'close' substrings in JSON keys (open checkpoint invariant)
    dumped = json.dumps(picks, ensure_ascii=False)
    assert "close_pct" not in dumped and "close_mean" not in dumped and "close_median" not in dumped


def test_shadow_ledger_record_and_t1_settlement():
    r = _FakeRedis()
    decision = {
        "trade_date": "2026-11-24",
        "env": {"regime": "DEEP_GREEN", "regime_label": "深绿·白线稳+放量(重仓进攻)"},
        "picks": [
            {"symbol": "002912", "name": "中新赛克", "theme": "AI安全", "ask1_price": 10.00, "std_weight_pct": 40},
            {"symbol": "002212", "name": "天融信", "theme": "AI安全", "ask1_price": 10.00, "std_weight_pct": 40},
        ],
    }
    assert record_shadow_ledger(r, decision) is True
    settled = settle_previous_shadow_ledger(
        redis_client=r,
        trade_date="2026-11-25",
        auction_price_by_symbol={"002912": 10.50, "002212": 9.90},
    )
    assert settled is not None
    assert settled["entry_date"] == "2026-11-24"
    assert settled["settle_date"] == "2026-11-25"
    # +5.0% - 0.1% = +4.9%, -1.0% - 0.1% = -1.1% -> mean = +1.9%
    assert settled["mean_net_pct"] == 1.9


def test_open_email_renders_compass_under_30kb_with_no_6digit_codes():
    import re
    from engine_core.email_board import render_email_board

    report = {
        "checkpoint": "0945",
        "chapters": {"market_price_volume": {"amount_yuan": 2.5e11}, "emerging": [], "extremes_board": {"open": {"rows": []}}},
        "open_relay_compass": {
            "schema": "OpenRelayCompassV1",
            "trade_date": "2026-11-02",
            "env": {
                "regime": "DEEP_GREEN",
                "regime_label": "深绿·白线稳+放量(重仓进攻)",
                "max_total_weight_pct": 80,
                "max_single_weight_pct": 40,
                "trial_single_weight_pct": 20,
                "yw_spread_pp": -0.35,
                "vol_ratio_5d": 1.12,
                "y2_pct": 3.4,
                "yr_red_pct": 62.0,
            },
            "buckets": [{"theme": "数据采集", "auc_rank": 18, "breadth_surge_pp": 24.5, "relay_ratio": 1.35}],
            "picks": [{
                "symbol": "002354", "name": "天娱数科", "board": "主板", "theme": "数据采集",
                "p0945_pct": 0.85, "acc_35_45_pp": 0.92, "prev_pct": -2.1, "prev_amount_yi": 11.2,
                "tags": ["★昨DDE吸筹", "★竞价静默"], "std_weight_pct": 40, "trial_weight_pct": 20,
            }],
            "watch_picks": [],
        },
    }
    html_doc, plain = render_email_board(report, "open", "2026-11-02")
    assert 'data-section="open-relay-alert"' in html_doc
    assert 'data-section="open-relay-compass"' in html_doc
    assert 'data-item="open-relay-pick"' in html_doc
    assert "天娱数科" in html_doc
    assert "深绿进攻信号" in html_doc
    assert len(html_doc.encode("utf-8")) < 30_000
    assert not re.search(r"(?<!\d)\d{6}(?!\d)", html_doc + plain)

