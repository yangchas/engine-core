"""Describe supplied auction prices by plate; no acquisition or side effects."""
from collections import defaultdict
from html import escape
from math import isfinite
from statistics import mean

from .auction_theme_cards import non_theme_reason
from .data_pipeline.auction_seal_facts import classify_limit


def build_auction_plate_view(auction_rows, stock_plates, *, tag, names=None, caps=None,
                             hot_rows=None, previous_rows=None, listings=None,
                             trading_dates=None, trade_date=None, min_members=10,
                             top_n=15, pool_rows=None, reason_rows=None,
                             hide_non_theme=True, min_limit_lift=2.0, min_limit_count=2):
    for key, value in (('min_members', min_members), ('top_n', top_n), ('min_limit_count', min_limit_count)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('invalid_' + key)
    if isinstance(min_limit_lift, bool) or not isinstance(min_limit_lift, (int, float)) or not isfinite(min_limit_lift) or min_limit_lift < 0:
        raise ValueError('invalid_min_limit_lift')
    quotes = {r['symbol']: r for r in auction_rows if r.get('symbol')}
    priced = {s: r for s, r in quotes.items() if r.get('price_milli') is not None}
    valid = {s: r for s, r in priced.items() if r.get('change_bp') is not None}
    market = mean(r['change_bp'] for r in valid.values()) if valid else None
    market_amount = sum(r['amount_yuan'] for r in priced.values() if r.get('amount_yuan') is not None)
    market_amount_complete = all(r.get('amount_yuan') is not None for r in priced.values())
    members = defaultdict(set)
    for symbol, plates in stock_plates.items():
        for plate in plates:
            code = plate.get('plate_code') if isinstance(plate, dict) else plate
            if code is not None:
                members[str(code)].add(symbol)
    hot = {str(r['plate_code']): r for r in (hot_rows or []) if r.get('plate_code') is not None}
    previous = None if previous_rows is None else {r['symbol']: r for r in previous_rows if r.get('symbol')}
    cap_rows = (caps.get('rows', caps) if caps else {})
    limit_available = bool(trade_date and trading_dates is not None and listings is not None)
    limits = {s: classify_limit(trade_date, s, r, listings.get(s), trading_dates)
              for s, r in priced.items()} if limit_available else {}
    market_ratios = {side: (sum(v == 'limit_' + side for v in limits.values()) / len(priced)
                            if limit_available and priced else None) for side in ('up', 'down')}
    result = []
    for code, symbols in sorted(members.items()):
        selected = [valid[s] for s in sorted(symbols & valid.keys())]
        n = len(selected)
        if n < max(1, min_members):
            continue
        priced_members = [priced[s] for s in sorted(symbols & priced.keys())]
        amounts = [r.get('amount_yuan') for r in priced_members]
        amount = sum(amounts) if all(x is not None for x in amounts) else None
        weighted = [r for r in selected if r.get('amount_yuan') is not None]
        denominator = sum(r['amount_yuan'] for r in weighted)
        weighted_bp = (sum(r['change_bp'] * r['amount_yuan'] for r in weighted) / denominator
                       if len(weighted) == n and denominator else None)
        avg = mean(r['change_bp'] for r in selected)
        strong = sum(r['change_bp'] >= 300 for r in selected)
        weak = sum(r['change_bp'] <= -300 for r in selected)
        middle = n - strong - weak
        h = hot.get(code, {})
        name = (names or {}).get(code) or h.get('name') or code
        ranked = sorted((r for r in selected if isinstance(cap_rows.get(r['symbol']), dict)
                         and cap_rows[r['symbol']].get('float_cap_yuan') is not None),
                        key=lambda r: (-cap_rows[r['symbol']]['float_cap_yuan'], r['symbol']))[:3]
        anchors = [dict(symbol=r['symbol'], change_bp=r['change_bp'], amount_yuan=r.get('amount_yuan'),
                        float_cap_yuan=cap_rows[r['symbol']]['float_cap_yuan']) for r in ranked]
        delta = None
        if previous is not None and amount is not None:
            prev_members = [previous[s] for s in sorted(symbols & previous.keys())
                            if previous[s].get('price_milli') is not None]
            if prev_members and all(r.get('amount_yuan') is not None for r in prev_members):
                delta = amount - sum(r['amount_yuan'] for r in prev_members)
        row = dict(plate_code=code, name=name, non_theme_reason=non_theme_reason(name),
                   member_count=n, mean_bp=avg, r_bp=avg-market, weighted_bp=weighted_bp,
                   strong_count=strong, middle_count=middle, weak_count=weak,
                   strong_share=strong/n, middle_share=middle/n, weak_share=weak/n,
                   limit_up_count=sum(limits.get(r['symbol']) == 'limit_up' for r in priced_members) if limit_available else None,
                   limit_down_count=sum(limits.get(r['symbol']) == 'limit_down' for r in priced_members) if limit_available else None,
                   limit_unknown_count=sum(limits.get(r['symbol']) in ('unknown_listing', 'unknown_price') for r in priced_members) if limit_available else None,
                   amount_yuan=amount, amount_share=(amount/market_amount if amount is not None and market_amount_complete and market_amount else None),
                   amount_delta_yuan=delta, anchors=anchors,
                   anchors_status=('caps_unavailable' if not cap_rows else ('available' if anchors else 'caps_missing')),
                   anchor_delta_bp=(mean(a['change_bp'] for a in anchors)-avg if anchors else None),
                   kaipan_rank=h.get('rank'), kaipan_strength=h.get('strength'), kaipan_change_pct=h.get('change_pct'))
        for side in ('up', 'down'):
            count = row['limit_' + side + '_count']
            ratio = count / len(priced_members) if count is not None and priced_members else None
            market_ratio = market_ratios[side]
            row['limit_' + side + '_ratio'] = ratio
            row['lift_limit_' + side] = ratio / market_ratio if ratio is not None and market_ratio else None
        result.append(row)
    eligible = [r for r in result if not hide_non_theme or not r['non_theme_reason']]
    rising = sorted(eligible, key=lambda r: (-r['r_bp'], r['plate_code']))[:top_n]
    falling = sorted(eligible, key=lambda r: (r['r_bp'], r['plate_code']))[:top_n]
    rankings = {}
    filtered = {}
    notes = {}
    for side in ('up', 'down'):
        candidates = [r for r in eligible if (r['limit_' + side + '_count'] or 0) > 0]
        market_count = sum(v == 'limit_' + side for v in limits.values())
        insufficient = limit_available and market_count < min_limit_count
        notes[side] = f'样本不足：全市场{market_count}只，至少需要{min_limit_count}只' if insufficient else ''
        qualified = [r for r in candidates if not insufficient and r['limit_' + side + '_count'] >= min_limit_count and r['lift_limit_' + side] is not None and r['lift_limit_' + side] >= min_limit_lift]
        filtered[side] = len(candidates) - len(qualified)
        rankings[side] = sorted(qualified, key=lambda r: (-r['lift_limit_' + side], -r['limit_' + side + '_count'], -r['r_bp'] if side == 'up' else r['r_bp'], r['plate_code']))[:top_n]
    indicative = sum(r.get('src') == 'indicative_bid_ask_mid' for r in priced.values())
    share = indicative/len(priced) if priced else None
    note = '指示价，终局会变' if tag in ('0920', '0924') else (f'指示买卖中间价来源占有价股票 {share:.1%}' if tag == '0925' and indicative else '')
    return dict(tag=tag, price_note=note, indicative_share=share, market_bp=market,
                market_count=len(valid), market_amount_yuan=market_amount if market_amount_complete else None,
                market_limit_up_ratio=market_ratios['up'], market_limit_down_ratio=market_ratios['down'],
                limit_up_note=notes['up'], limit_down_note=notes['down'],
                limit_up_filtered_count=filtered['up'], limit_down_filtered_count=filtered['down'],
                plates=result, rising=rising, falling=falling, limit_up=rankings['up'], limit_down=rankings['down'])


def render_auction_plate_html(view):
    def cell(value):
        return escape('—' if value is None else str(value))
    parts = [f"<p>{cell(view.get('tag'))} {cell(view.get('price_note'))}；板块相对大盘平均涨幅（bp），涨停只数为触及限价的股票数。</p>"]
    for key, title in (('rising', '涨幅榜'), ('limit_up', '涨停榜'), ('falling', '跌幅榜'), ('limit_down', '跌停榜')):
        side = key.removeprefix('limit_') if key.startswith('limit_') else None
        extra = '<th>占比</th><th>倍数</th>' if side else ''
        if side and view.get(key + '_note'):
            parts.append(f'<p>{cell(view[key + "_note"])}</p>')
        parts.append(f'<h2>{title}</h2><table><thead><tr><th>板块</th><th>板块相对大盘平均涨幅</th><th>涨停只数</th><th>跌停只数</th>{extra}</tr></thead><tbody>')
        for row in view.get(key, []):
            fields = ('name', 'r_bp', 'limit_up_count', 'limit_down_count') + (('limit_' + side + '_ratio', 'lift_limit_' + side) if side else ())
            parts.append('<tr>' + ''.join('<td>' + cell(row.get(k)) + '</td>' for k in fields) + '</tr>')
        parts.append('</tbody></table>')
    return ''.join(parts)
