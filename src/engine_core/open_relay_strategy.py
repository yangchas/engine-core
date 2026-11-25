"""09:45 Open Environment Compass, Anti-Consensus Relay Buckets, Buy-Point C Selection & T+1 Shadow Ledger.

Strictly zero-lookahead (PIT):
- Prior-day kline / DDE / 20-day range use strictly trade_date < D.
- Intraday snapshots use market_data1.stock_tick_v2 with ts <= D 09:45:30.
- Stock universe strictly limited to Main Board (00/60) + ChiNext/GEM (300/301),
  excluding STAR (688/689), BSE (8/4/92), ST, suspended (amt == 0), and sealed-up (ap1 == 0).
- Missing != Zero: absent upstream inputs produce None / degraded reasons, never fabricated zeros.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .auction_theme_buckets import normalize_theme_label
from .auction_theme_cards import non_theme_reason

TZ = timezone(timedelta(hours=8))
SHADOW_KEY_PREFIX = "core:shadow:open_relay:"
SHADOW_INDEX_KEY = "core:shadow:open_relay:dates"
COST_PP = 0.10  # 10 bps round-trip commission + stamp tax


def is_tradable_board(symbol: str) -> bool:
    """Allow Main Board (00/60) + ChiNext/GEM (300/301) only; exclude STAR (688/689) and BSE."""
    s = str(symbol or "").strip().zfill(6)
    return s.startswith(("00", "60", "300", "301"))


def _limit_band(symbol: str, is_st: bool = False) -> float:
    if is_st:
        return 0.05
    if symbol.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _is_st_name(name: Any) -> bool:
    s = str(name or "").upper().strip()
    return s.startswith(("ST", "*ST", "S*ST", "SST"))


def evaluate_open_environment(
    *,
    ticks_0925: Mapping[str, Mapping[str, Any]],
    ticks_0945: Mapping[str, Mapping[str, Any]],
    prev_kline: Mapping[str, Mapping[str, Any]],
    yest_streaks: Mapping[str, int],
    yest_broken_syms: set[str],
    yest_rotten_syms: set[str],
    prior_5d_mkt_amt_0945: Sequence[float],
    st_syms: set[str],
) -> dict[str, Any]:
    """Compute real-time 09:45:30 global market & style compass and position regime."""
    degraded: list[str] = []
    if not ticks_0945:
        return {
            "status": "DEGRADED",
            "regime": "UNKNOWN",
            "regime_label": "数据缺失·观望",
            "max_total_weight_pct": 0,
            "max_single_weight_pct": 0,
            "trial_single_weight_pct": 0,
            "degraded_reasons": ["TICKS_0945_MISSING"],
        }

    pct45: dict[str, float] = {}
    sealed_up: set[str] = set()
    broken_up: set[str] = set()
    tot_amt45 = 0.0

    for sym, row in ticks_0945.items():
        px = row.get("px_milli") or 0
        pc = row.get("pc_milli") or 0
        if px <= 0 or pc <= 0:
            continue
        pct45[sym] = 100.0 * (px - pc) / pc
        tot_amt45 += float(row.get("amt_yuan") or 0)
        band = _limit_band(sym, sym in st_syms)
        lu = round(pc * (1.0 + band) / 10.0) * 10
        ap1 = row.get("ap1_milli") or 0
        av1 = row.get("av1") or 0
        bp1 = row.get("bp1_milli") or 0
        mh = row.get("max_h_milli") or px
        su = (px >= lu - 10) and (ap1 == 0 or av1 == 0) and (bp1 >= lu - 10)
        if su:
            sealed_up.add(sym)
        elif mh >= lu - 10:
            broken_up.add(sym)

    if not pct45:
        return {
            "status": "DEGRADED",
            "regime": "UNKNOWN",
            "regime_label": "有效行情不足·观望",
            "max_total_weight_pct": 0,
            "max_single_weight_pct": 0,
            "trial_single_weight_pct": 0,
            "degraded_reasons": ["VALID_PCT45_EMPTY"],
        }

    mkt_eq = statistics.fmean(pct45.values())
    float_cap: dict[str, float] = {}
    for sym, krow in prev_kline.items():
        direct_cap = float(krow.get("float_cap_yuan") or 0.0)
        if direct_cap > 0:
            float_cap[sym] = direct_cap
            continue
        amt = float(krow.get("amount_yuan") or 0.0)
        turn = float(krow.get("turnover_pct") or 0.0)
        if amt > 0 and turn > 0:
            float_cap[sym] = amt / (turn / 100.0)

    cap_syms = [s for s in pct45 if s in float_cap]
    cap_sum = sum(float_cap[s] for s in cap_syms)
    if cap_sum > 0:
        mkt_cap_w = sum(float_cap[s] * pct45[s] for s in cap_syms) / cap_sum
    else:
        mkt_cap_w = None
        degraded.append("FLOAT_CAP_MISSING")

    yw_spread = (mkt_eq - mkt_cap_w) if mkt_cap_w is not None else None
    valid_prior_amts = [float(x) for x in prior_5d_mkt_amt_0945 if x and x > 0]
    vol_ratio_5d = (tot_amt45 / statistics.fmean(valid_prior_amts)) if len(valid_prior_amts) >= 3 else None
    if vol_ratio_5d is None:
        degraded.append("VOL_5D_BASELINE_SHORT")

    up_ratio = sum(1 for v in pct45.values() if v > 0) / len(pct45)

    # Yesterday streak >= 2 ladder feedback
    s2_syms = [s for s, st in yest_streaks.items() if st >= 2 and s in pct45 and s not in st_syms]
    y2_pct = statistics.fmean(pct45[s] for s in s2_syms) if s2_syms else None
    y2_nuke = (sum(1 for s in s2_syms if pct45[s] <= -5.0) / len(s2_syms)) if s2_syms else None
    y2_promo = (sum(1 for s in s2_syms if s in sealed_up) / len(s2_syms)) if s2_syms else None

    # Yesterday broken-board & rotten-board feedback
    yb_pcts = [pct45[s] for s in yest_broken_syms if s in pct45]
    yb_red_ratio = (sum(1 for v in yb_pcts if v > 0) / len(yb_pcts)) if yb_pcts else None
    yr_pcts = [pct45[s] for s in yest_rotten_syms if s in pct45]
    yr_red_ratio = (sum(1 for v in yr_pcts if v > 0) / len(yr_pcts)) if yr_pcts else None

    # Regime decision (strict natural boundaries: yw <= 0, vol5 >= 1.0)
    ladder_ok = (y2_pct is not None and y2_pct > 2.0 and (y2_nuke is None or y2_nuke < 0.15))
    broken_ok = (yb_red_ratio is not None and yb_red_ratio >= 0.40)
    rotten_ok = (yr_red_ratio is not None and yr_red_ratio >= 0.50)

    if yw_spread is None:
        regime, label = "YELLOW", "黄灯·缺市值基线(观望)"
        max_total, max_single, trial_single = 0, 0, 0
    elif yw_spread > 0 and vol_ratio_5d is not None and vol_ratio_5d < 1.0:
        regime, label = "RED", "红灯·小票一致+缩量(铁血空仓)"
        max_total, max_single, trial_single = 0, 0, 0
    elif yw_spread <= 0 and vol_ratio_5d is not None and vol_ratio_5d >= 1.0:
        regime, label = "DEEP_GREEN", "深绿·白线稳+放量(重仓进攻)"
        max_total, max_single, trial_single = 80, 40, 20
    elif yw_spread <= 0 and (ladder_ok or broken_ok or rotten_ok):
        regime, label = "LIGHT_GREEN", "浅绿·白线稳+情绪承接(中仓出击)"
        max_total, max_single, trial_single = 60, 35, 18
    else:
        regime, label = "YELLOW", "黄灯·震荡分歧(原则观望)"
        max_total, max_single, trial_single = 0, 0, 0

    return {
        "status": "READY" if not degraded else "PARTIAL",
        "regime": regime,
        "regime_label": label,
        "max_total_weight_pct": max_total,
        "max_single_weight_pct": max_single,
        "trial_single_weight_pct": trial_single,
        "mkt_eq_pct": round(mkt_eq, 2),
        "mkt_cap_w_pct": round(mkt_cap_w, 2) if mkt_cap_w is not None else None,
        "yw_spread_pp": round(yw_spread, 2) if yw_spread is not None else None,
        "vol_ratio_5d": round(vol_ratio_5d, 2) if vol_ratio_5d is not None else None,
        "tot_amt_0945_yi": round(tot_amt45 / 1e8, 1),
        "up_ratio_pct": round(up_ratio * 100.0, 1),
        "y2_count": len(s2_syms),
        "y2_pct": round(y2_pct, 2) if y2_pct is not None else None,
        "y2_nuke_pct": round(y2_nuke * 100.0, 1) if y2_nuke is not None else None,
        "y2_promo_pct": round(y2_promo * 100.0, 1) if y2_promo is not None else None,
        "yb_red_pct": round(yb_red_ratio * 100.0, 1) if yb_red_ratio is not None else None,
        "yr_red_pct": round(yr_red_ratio * 100.0, 1) if yr_red_ratio is not None else None,
        "sealed_up_count": len(sealed_up),
        "broken_up_count": len(broken_up),
        "degraded_reasons": degraded,
    }


def select_anti_consensus_buckets(
    *,
    theme_members: Mapping[str, set[str]],
    ticks_0925: Mapping[str, Mapping[str, Any]],
    ticks_0935: Mapping[str, Mapping[str, Any]],
    ticks_0945: Mapping[str, Mapping[str, Any]],
    prior_pos20: Mapping[str, float],
    prior_5d_theme_amt25: Mapping[str, Sequence[float]] | None = None,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Rank 09:25 buckets with production alpha=0.50 crowding, then pick Top N anti-consensus relay buckets."""
    prior_5d_theme_amt25 = prior_5d_theme_amt25 or {}
    raw_buckets: list[tuple[float, str, list[str], float, float, float, float | None]] = []

    for theme, members in theme_members.items():
        m45 = [s for s in members if s in ticks_0945 and (ticks_0945[s].get("px_milli") or 0) > 0 and (ticks_0945[s].get("pc_milli") or 0) > 0]
        if len(m45) < 5:
            continue
        amt25 = sum(float(ticks_0925[s].get("amt_yuan") or 0) for s in m45 if s in ticks_0925)
        amt35 = sum(float(ticks_0935[s].get("amt_yuan") or 0) for s in m45 if s in ticks_0935)
        amt45 = sum(float(ticks_0945[s].get("amt_yuan") or 0) for s in m45)
        if amt25 < 3e7 and amt35 < 5e7:
            continue
        hist = [float(x) for x in prior_5d_theme_amt25.get(theme, ()) if x and x > 0]
        crowd5 = (amt25 / statistics.fmean(hist)) if len(hist) >= 3 else None
        mult = math.sqrt(max(0.5, min(4.0, crowd5))) if crowd5 is not None else 1.0
        raw_buckets.append((amt25 * mult, theme, m45, amt25, amt35, amt45, crowd5))

    raw_buckets.sort(key=lambda x: x[0], reverse=True)
    candidates: list[dict[str, Any]] = []

    for auc_rank, (_, theme, m45, amt25, amt35, amt45, crowd5) in enumerate(raw_buckets, 1):
        if auc_rank <= 10:
            continue  # Exclude 09:25 consensus Top 10
        p35_list = [
            100.0 * (ticks_0935[s]["px_milli"] - ticks_0935[s]["pc_milli"]) / ticks_0935[s]["pc_milli"]
            for s in m45
            if s in ticks_0935 and (ticks_0935[s].get("px_milli") or 0) > 0 and (ticks_0935[s].get("pc_milli") or 0) > 0
        ]
        p45_list = [
            100.0 * (ticks_0945[s]["px_milli"] - ticks_0945[s]["pc_milli"]) / ticks_0945[s]["pc_milli"]
            for s in m45
        ]
        if not p35_list or not p45_list:
            continue
        p35 = statistics.fmean(p35_list)
        p45 = statistics.fmean(p45_list)
        up35 = sum(1 for x in p35_list if x > 0) / len(p35_list)
        up45 = sum(1 for x in p45_list if x > 0) / len(p45_list)
        bsurge = up45 - up35
        denom = amt35 - amt25
        relay = (amt45 - amt35) / denom if denom > 0 else 0.0
        pos_vals = [prior_pos20[s] for s in m45 if s in prior_pos20]
        med_pos20 = statistics.median(pos_vals) if pos_vals else None

        if (
            p35 < 1.5
            and up35 < 0.75
            and p45 > p35
            and bsurge > 0
            and relay >= 1.0
            and med_pos20 is not None
            and med_pos20 >= 30.0
        ):
            candidates.append({
                "theme": theme,
                "auc_rank": auc_rank,
                "member_count": len(m45),
                "p0935_pct": round(p35, 2),
                "p0945_pct": round(p45, 2),
                "acc_35_45_pp": round(p45 - p35, 2),
                "up35_pct": round(up35 * 100.0, 1),
                "up45_pct": round(up45 * 100.0, 1),
                "breadth_surge_pp": round(bsurge * 100.0, 1),
                "relay_ratio": round(relay, 2),
                "prior_pos20_pct": round(med_pos20, 1),
                "crowd5": round(crowd5, 2) if crowd5 is not None else None,
            })

    candidates.sort(key=lambda x: (x["breadth_surge_pp"], x["acc_35_45_pp"]), reverse=True)
    return candidates[:top_n]


def select_buy_point_c_stocks(
    *,
    top_buckets: Sequence[Mapping[str, Any]],
    theme_members: Mapping[str, set[str]],
    ticks_0925: Mapping[str, Mapping[str, Any]],
    ticks_0935: Mapping[str, Mapping[str, Any]],
    ticks_0945: Mapping[str, Mapping[str, Any]],
    prev_kline: Mapping[str, Mapping[str, Any]],
    prev_dde: Mapping[str, float],
    stock_names: Mapping[str, str],
    st_syms: set[str],
    max_stocks: int = 2,
    max_single_weight_pct: int = 40,
    trial_single_weight_pct: int = 20,
    max_total_weight_pct: int = 80,
) -> list[dict[str, Any]]:
    """Select Main + GEM Buy-Point C stocks inside Top anti-consensus buckets."""
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()

    for b in top_buckets:
        theme = str(b.get("theme") or "")
        for sym in theme_members.get(theme, ()):
            if sym in seen or sym in st_syms or not is_tradable_board(sym):
                continue
            name = str(stock_names.get(sym) or "").strip()
            if _is_st_name(name):
                continue
            r45 = ticks_0945.get(sym)
            kp = prev_kline.get(sym)
            if not r45 or not kp:
                continue
            amt45 = float(r45.get("amt_yuan") or 0.0)
            ap1_m = int(r45.get("ap1_milli") or 0)
            av1 = int(r45.get("av1") or 0)
            px_m = int(r45.get("px_milli") or 0)
            pc_m = int(r45.get("pc_milli") or 0)
            # Strictly tradable at 09:45:30 (not suspended, not limit-up sealed)
            if amt45 <= 0 or ap1_m <= 0 or av1 <= 0 or px_m <= 0 or pc_m <= 0:
                continue

            pct45 = 100.0 * (px_m - pc_m) / pc_m
            r35 = ticks_0935.get(sym)
            if r35 and (r35.get("px_milli") or 0) > 0 and (r35.get("pc_milli") or 0) > 0:
                pct35 = 100.0 * (r35["px_milli"] - r35["pc_milli"]) / r35["pc_milli"]
            else:
                pct35 = pct45
            acc = pct45 - pct35

            prev_pct = float(kp.get("pct_chg") or 0.0)
            prev_amt = float(kp.get("amount_yuan") or 0.0)
            if not (prev_pct < 0.0 and prev_amt >= 5e8 and pct45 < 2.0 and acc > 0.0):
                continue

            seen.add(sym)
            amt25 = float((ticks_0925.get(sym) or {}).get("amt_yuan") or 0.0)
            ddje = prev_dde.get(sym)
            amt45_ratio = (amt45 / prev_amt * 100.0) if prev_amt > 0 else None
            po = float(kp.get("open") or 0.0)
            pl = float(kp.get("low") or 0.0)
            pcl = float(kp.get("close") or 0.0)
            ppc = float(kp.get("pre_close") or 0.0)
            prev_lower_sh = (100.0 * (min(po, pcl) - pl) / ppc) if (po > 0 and pl > 0 and pcl > 0 and ppc > 0) else 0.0

            tags: list[str] = []
            if ddje is not None and ddje > 0:
                tags.append("★昨DDE吸筹")
            if amt25 < 1e7:
                tags.append("★竞价静默")
            if amt45_ratio is not None and amt45_ratio >= 15.0:
                tags.append("★09:45放量")
            if prev_lower_sh >= 1.0:
                tags.append("★昨下影承接")

            cands.append({
                "symbol": sym,
                "name": name or "名称未知",
                "board": "创业板" if sym.startswith(("300", "301")) else "主板",
                "theme": theme,
                "ask1_price": round(ap1_m / 1000.0, 2),
                "p0945_pct": round(pct45, 2),
                "acc_35_45_pp": round(acc, 2),
                "prev_pct": round(prev_pct, 2),
                "prev_amount_yi": round(prev_amt / 1e8, 1),
                "amt0945_yi": round(amt45 / 1e8, 2),
                "amt0925_wan": round(amt25 / 1e4, 0),
                "prev_dde_wan": round(ddje / 1e4, 0) if ddje is not None else None,
                "tags": tags,
            })

    cands.sort(key=lambda x: (x["prev_amount_yi"], x["amt0945_yi"]), reverse=True)
    picked = cands[:max_stocks]
    if picked and max_total_weight_pct > 0:
        per_std = min(max_single_weight_pct, max_total_weight_pct // len(picked))
        per_trial = min(trial_single_weight_pct, max(1, (max_total_weight_pct // 2) // len(picked)))
    else:
        per_std, per_trial = 0, 0

    for item in picked:
        item["std_weight_pct"] = per_std
        item["trial_weight_pct"] = per_trial
    return picked


def build_open_relay_decision(
    *,
    trade_date: str,
    prev_trade_date: str,
    theme_members: Mapping[str, set[str]],
    ticks_0925: Mapping[str, Mapping[str, Any]],
    ticks_0935: Mapping[str, Mapping[str, Any]],
    ticks_0945: Mapping[str, Mapping[str, Any]],
    prev_kline: Mapping[str, Mapping[str, Any]],
    prior_pos20: Mapping[str, float],
    yest_streaks: Mapping[str, int],
    yest_broken_syms: set[str],
    yest_rotten_syms: set[str],
    prior_5d_mkt_amt_0945: Sequence[float],
    prior_5d_theme_amt25: Mapping[str, Sequence[float]] | None,
    prev_dde: Mapping[str, float],
    stock_names: Mapping[str, str],
    st_syms: set[str],
    max_stocks: int = 2,
) -> dict[str, Any]:
    """Orchestrate 09:45 environment compass + anti-consensus buckets + Buy-Point C stocks."""
    env = evaluate_open_environment(
        ticks_0925=ticks_0925,
        ticks_0945=ticks_0945,
        prev_kline=prev_kline,
        yest_streaks=yest_streaks,
        yest_broken_syms=yest_broken_syms,
        yest_rotten_syms=yest_rotten_syms,
        prior_5d_mkt_amt_0945=prior_5d_mkt_amt_0945,
        st_syms=st_syms,
    )
    top_buckets = select_anti_consensus_buckets(
        theme_members=theme_members,
        ticks_0925=ticks_0925,
        ticks_0935=ticks_0935,
        ticks_0945=ticks_0945,
        prior_pos20=prior_pos20,
        prior_5d_theme_amt25=prior_5d_theme_amt25,
        top_n=3,
    )
    is_green = env.get("regime") in {"DEEP_GREEN", "LIGHT_GREEN"}
    picks = select_buy_point_c_stocks(
        top_buckets=top_buckets,
        theme_members=theme_members,
        ticks_0925=ticks_0925,
        ticks_0935=ticks_0935,
        ticks_0945=ticks_0945,
        prev_kline=prev_kline,
        prev_dde=prev_dde,
        stock_names=stock_names,
        st_syms=st_syms,
        max_stocks=max_stocks,
        max_single_weight_pct=int(env.get("max_single_weight_pct") or 0) if is_green else 0,
        trial_single_weight_pct=int(env.get("trial_single_weight_pct") or 0) if is_green else 0,
        max_total_weight_pct=int(env.get("max_total_weight_pct") or 0) if is_green else 0,
    )
    return {
        "schema": "OpenRelayCompassV1",
        "trade_date": trade_date,
        "prev_trade_date": prev_trade_date,
        "cutoff": "09:45:30",
        "env": env,
        "buckets": top_buckets,
        "picks": picks if is_green else [],
        "watch_picks": picks if not is_green else [],
        "exit_rule": "次日 09:25 集合竞价直接卖出（主板+创业板，自动剔除科创板/北交所/ST）",
    }


def record_shadow_ledger(redis_client: Any, decision: Mapping[str, Any]) -> bool:
    """Persist green-light shadow picks to Redis for automatic T+1 09:26 auction settlement."""
    if redis_client is None:
        return False
    trade_date = str(decision.get("trade_date") or "")
    env = decision.get("env") if isinstance(decision.get("env"), Mapping) else {}
    picks = decision.get("picks") if isinstance(decision.get("picks"), list) else []
    if not trade_date or env.get("regime") not in {"DEEP_GREEN", "LIGHT_GREEN"} or not picks:
        return False
    payload = {
        "trade_date": trade_date,
        "regime": env.get("regime"),
        "regime_label": env.get("regime_label"),
        "recorded_at": datetime.now(TZ).isoformat(),
        "picks": [
            {
                "symbol": p.get("symbol"),
                "name": p.get("name"),
                "theme": p.get("theme"),
                "ask1_price": p.get("ask1_price"),
                "std_weight_pct": p.get("std_weight_pct"),
            }
            for p in picks
        ],
    }
    try:
        key = f"{SHADOW_KEY_PREFIX}{trade_date}"
        redis_client.set(key, json.dumps(payload, ensure_ascii=False), ex=86400 * 90)
        redis_client.zadd(SHADOW_INDEX_KEY, {trade_date: int(trade_date.replace("-", ""))})
        return True
    except Exception:
        return False


def settle_previous_shadow_ledger(
    *,
    redis_client: Any,
    trade_date: str,
    auction_price_by_symbol: Mapping[str, float],
) -> dict[str, Any] | None:
    """At D+1 09:26 auction, settle the most recent unsettled shadow entry strictly < trade_date."""
    if redis_client is None or not auction_price_by_symbol:
        return None
    try:
        dates = redis_client.zrevrange(SHADOW_INDEX_KEY, 0, 10)
    except Exception:
        return None
    prev_entry = None
    for raw_d in dates or ():
        d_str = raw_d.decode("utf-8") if isinstance(raw_d, bytes) else str(raw_d)
        if d_str >= trade_date:
            continue
        raw = redis_client.get(f"{SHADOW_KEY_PREFIX}{d_str}")
        if not raw:
            continue
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        obj = json.loads(text)
        if not obj.get("settled_date") or obj.get("settled_date") == trade_date:
            prev_entry = obj
            break
    if not prev_entry:
        return None

    settled_rows = []
    net_rets = []
    for p in prev_entry.get("picks") or []:
        sym = str(p.get("symbol") or "").zfill(6)
        buy = float(p.get("ask1_price") or 0.0)
        sell = auction_price_by_symbol.get(sym)
        if buy > 0 and sell is not None and sell > 0:
            net_pct = round(100.0 * (sell - buy) / buy - COST_PP, 2)
            net_rets.append(net_pct)
        else:
            net_pct = None
        settled_rows.append({
            "symbol": sym,
            "name": p.get("name"),
            "theme": p.get("theme"),
            "buy_ask1": buy,
            "sell_auction": round(sell, 2) if sell is not None else None,
            "net_pct": net_pct,
        })

    mean_net = round(statistics.fmean(net_rets), 2) if net_rets else None
    settlement = {
        "entry_date": prev_entry.get("trade_date"),
        "settle_date": trade_date,
        "regime_label": prev_entry.get("regime_label"),
        "mean_net_pct": mean_net,
        "rows": settled_rows,
    }
    try:
        prev_entry["settled_date"] = trade_date
        prev_entry["settlement"] = settlement
        redis_client.set(
            f"{SHADOW_KEY_PREFIX}{prev_entry['trade_date']}",
            json.dumps(prev_entry, ensure_ascii=False),
            ex=86400 * 90,
        )
    except Exception:
        pass
    return settlement


def compute_open_relay_compass_td(
    *,
    trade_date: str,
    plate_members_rows: Sequence[Mapping[str, Any]],
    stock_names: Mapping[str, str],
    td_config: str | None = None,
    redis_client: Any = None,
    write_shadow: bool = True,
) -> dict[str, Any]:
    """Live/replay adapter: query TDengine stock_tick_v2 + strictly < D daily_kline_v1 & daily_dde_v1."""
    import taos  # local import to keep unit tests free of taos runtime dependency

    cfg = td_config or os.getenv("TD_CONFIG", "/home/exedev/validation/operator-runs/taos-cfg")
    conn = taos.connect(config=cfg)
    try:
        cur = conn.cursor()

        # 1. Theme members from strictly PIT plate_members_rows
        theme_members: dict[str, set[str]] = defaultdict(set)
        for row in plate_members_rows:
            if not isinstance(row, Mapping):
                continue
            sym = str(row.get("symbol") or row.get("code") or "").zfill(6)
            lbl = str(row.get("plate_member_label") or row.get("plate_name") or "").strip()
            tok = lbl.replace("、", ",").split(",")[0].strip()
            norm = normalize_theme_label(tok)
            if sym and norm and not non_theme_reason(norm, extended=True):
                theme_members[norm].add(sym)

        # 2. Strictly prior 25 calendar-window trading dates from daily_kline_v1 (ts < trade_date)
        start_dt = (datetime.fromisoformat(trade_date) - timedelta(days=45)).date().isoformat()
        cur.execute(
            f"SELECT symbol, ts, open, high, low, close, pre_close, pct_chg, amount_yuan, turnover_pct, suspended "
            f"FROM core_data.daily_kline_v1 "
            f"WHERE ts >= '{start_dt} 00:00:00' AND ts < '{trade_date} 00:00:00'"
        )
        kline_by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for sym, ts, o, h, l, cl, pc, pct, amt, turn, susp in cur.fetchall():
            if cl and pc and pc > 0:
                kline_by_day[str(ts)[:10]][sym] = {
                    "open": float(o or cl), "high": float(h or cl), "low": float(l or cl),
                    "close": float(cl), "pre_close": float(pc), "pct_chg": float(pct or 0.0),
                    "amount_yuan": float(amt or 0.0), "turnover_pct": float(turn or 0.0),
                    "suspended": bool(susp),
                }
        prior_dates = sorted(kline_by_day.keys())
        if not prior_dates:
            raise ValueError(f"NO_PRIOR_DAILY_KLINE_BEFORE_{trade_date}")
        prev_d = prior_dates[-1]
        prior20_dates = prior_dates[-20:]
        prior5_dates = prior_dates[-5:]

        # ST set from stock_names
        st_syms = {sym for sym, nm in stock_names.items() if _is_st_name(nm)}

        # Prior streaks (last 10 trading days strictly < D)
        prev_streak: dict[str, int] = {}
        for pd in prior_dates[-10:]:
            cur_s: dict[str, int] = {}
            for sym, r in kline_by_day[pd].items():
                if r["suspended"] or sym in st_syms:
                    continue
                band = _limit_band(sym, False)
                lim = round(r["pre_close"] * (1.0 + band), 2)
                if r["close"] >= lim - 0.015:
                    cur_s[sym] = prev_streak.get(sym, 0) + 1
            prev_streak = cur_s
        yest_streaks = prev_streak

        # Yesterday broken & rotten pools
        prev_kline = kline_by_day[prev_d]
        # Redis-first PIT float cap enrichment (core:view:stock_cap:<prev_d>, 5ms HGETALL)
        if redis_client is not None:
            try:
                raw_caps = redis_client.hgetall(f"core:view:stock_cap:{prev_d}")
                for k_sym, raw_val in (raw_caps or {}).items():
                    sym_str = (k_sym.decode("utf-8") if isinstance(k_sym, bytes) else str(k_sym)).zfill(6)
                    if sym_str not in prev_kline:
                        continue
                    v_text = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else str(raw_val)
                    cap_obj = json.loads(v_text)
                    fcap = cap_obj.get("float_cap_yuan") if isinstance(cap_obj, Mapping) else None
                    if isinstance(fcap, (int, float)) and fcap > 0:
                        prev_kline[sym_str]["float_cap_yuan"] = float(fcap)
            except Exception:
                pass
        yest_broken: set[str] = set()
        yest_rotten: set[str] = set()
        for sym, r in prev_kline.items():
            if r["suspended"] or sym in st_syms:
                continue
            band = _limit_band(sym, False)
            lim = round(r["pre_close"] * (1.0 + band), 2)
            if r["high"] >= lim - 0.015 and r["close"] < lim - 0.015:
                yest_broken.add(sym)
            elif sym in yest_streaks and r["low"] < r["close"] * 0.985 and r["open"] < r["close"] * 0.995:
                yest_rotten.add(sym)

        # Prior 20d range position strictly < D
        prior_pos20: dict[str, float] = {}
        for sym, r in prev_kline.items():
            closes = [kline_by_day[pd][sym]["close"] for pd in prior20_dates if sym in kline_by_day[pd]]
            if len(closes) >= 15:
                lo, hi = min(closes), max(closes)
                if hi > lo:
                    prior_pos20[sym] = (r["close"] - lo) / (hi - lo) * 100.0

        # D-1 DDE strictly on prev_d
        prev_dde: dict[str, float] = {}
        try:
            cur.execute(
                f"SELECT symbol, ddje FROM core_data.daily_dde_v1 "
                f"WHERE ts >= '{prev_d} 00:00:00' AND ts <= '{prev_d} 23:59:59'"
            )
            for sym, val in cur.fetchall():
                if val is not None:
                    prev_dde[sym] = float(val)
        except Exception:
            pass

        # 3. Fetch exact 09:25:00, 09:35:00, 09:45:30 ticks for trade_date
        def _fetch_cut(d: str, t: str) -> dict[str, dict[str, Any]]:
            cur.execute(
                f"SELECT symbol, LAST_ROW(px_milli), LAST_ROW(pc_milli), MAX(h_milli), LAST_ROW(amt_yuan), "
                f"LAST_ROW(bp1_milli), LAST_ROW(ap1_milli), LAST_ROW(bv1), LAST_ROW(av1) "
                f"FROM market_data1.stock_tick_v2 "
                f"WHERE ts >= '{d} 09:15:00' AND ts <= '{d} {t}' GROUP BY symbol"
            )
            out: dict[str, dict[str, Any]] = {}
            for sym, px, pc, mh, amt, bp1, ap1, bv1, av1 in cur.fetchall():
                if px and pc and px > 0 and pc > 0:
                    out[sym] = {
                        "px_milli": int(px), "pc_milli": int(pc), "max_h_milli": int(mh or px),
                        "amt_yuan": int(amt or 0), "bp1_milli": int(bp1 or 0),
                        "ap1_milli": int(ap1 or 0), "bv1": int(bv1 or 0), "av1": int(av1 or 0),
                    }
            return out

        ticks_0925 = _fetch_cut(trade_date, "09:25:00")
        ticks_0935 = _fetch_cut(trade_date, "09:35:00")
        ticks_0945 = _fetch_cut(trade_date, "09:45:30")

        # 4. Prior 5-day global 09:45:30 market amount baseline (strictly < D)
        prior_5d_mkt_amt: list[float] = []
        prior_5d_theme_amt25: dict[str, list[float]] = defaultdict(list)
        for pd in prior5_dates:
            cur.execute(
                f"SELECT SUM(a) FROM ("
                f"SELECT LAST_ROW(amt_yuan) AS a FROM market_data1.stock_tick_v2 "
                f"WHERE ts >= '{pd} 09:15:00' AND ts <= '{pd} 09:45:30' GROUP BY symbol)"
            )
            r = cur.fetchall()
            if r and r[0][0] and float(r[0][0]) > 0:
                prior_5d_mkt_amt.append(float(r[0][0]))
            cur.execute(
                f"SELECT symbol, LAST_ROW(amt_yuan) FROM market_data1.stock_tick_v2 "
                f"WHERE ts >= '{pd} 09:15:00' AND ts <= '{pd} 09:25:00' GROUP BY symbol"
            )
            p25_map = {sym: float(a or 0) for sym, a in cur.fetchall() if a}
            if p25_map:
                for th, syms in theme_members.items():
                    s_amt = sum(p25_map.get(s, 0.0) for s in syms)
                    if s_amt > 0:
                        prior_5d_theme_amt25[th].append(s_amt)
    finally:
        conn.close()

    decision = build_open_relay_decision(
        trade_date=trade_date,
        prev_trade_date=prev_d,
        theme_members=theme_members,
        ticks_0925=ticks_0925,
        ticks_0935=ticks_0935,
        ticks_0945=ticks_0945,
        prev_kline=prev_kline,
        prior_pos20=prior_pos20,
        yest_streaks=yest_streaks,
        yest_broken_syms=yest_broken,
        yest_rotten_syms=yest_rotten,
        prior_5d_mkt_amt_0945=prior_5d_mkt_amt,
        prior_5d_theme_amt25=prior_5d_theme_amt25,
        prev_dde=prev_dde,
        stock_names=stock_names,
        st_syms=st_syms,
        max_stocks=2,
    )
    if write_shadow:
        decision["shadow_recorded"] = record_shadow_ledger(redis_client, decision)
    return decision
