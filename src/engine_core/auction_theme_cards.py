"""Build an independent, point-in-time auction theme preview from supplied facts."""
from collections import Counter, defaultdict
from html import escape
from statistics import mean

from .theme_taxonomy import build_taxonomy, build_theme_cards


def build_auction_theme_view(auction_rows, stock_plates, hot_rows, seal_rows, pool_rows, reason_rows, *,
                             names=None, boards=None, listings=None, trading_dates=None, trade_date=None,
                             previous_trade_date=None, caps=None, top_n_hot=40):
    if not isinstance(top_n_hot, int) or isinstance(top_n_hot, bool) or top_n_hot < 0:
        raise ValueError('invalid_top_n_hot')
    if not stock_plates:
        return {'status': 'unavailable', 'reason': 'mapping_unavailable'}
    members = defaultdict(set)
    for symbol, plates in stock_plates.items():
        for plate in plates:
            members[str(plate.get('plate_code') if isinstance(plate, dict) else plate)].add(symbol)
    hot = {str(row['plate_code']): row for row in hot_rows}
    plate_names = dict(names or {})
    plate_names.update({c: r['name'] for c, r in hot.items() if r.get('name')})
    pool = {r['symbol']: r for r in pool_rows if r.get('symbol')}
    reasons = defaultdict(set)
    for symbol, row in pool.items():
        code = row.get('main_reason_plate_code') or row.get('plate_code')
        if code:
            reasons[str(code)].add(symbol)
    for row in reason_rows:
        symbol = row.get('symbol')
        if symbol not in pool or (previous_trade_date and (row.get('event_date') or row.get('date')) != previous_trade_date):
            continue
        code = row.get('main_reason_plate_code') or row.get('plate_code')
        if code:
            reasons[str(code)].add(symbol)
    ranked_reasons = sorted(reasons, key=lambda c: (-len(reasons[c]), c))[:20]
    selected = {str(r['plate_code']) for r in sorted(hot_rows, key=lambda r: (r['rank'], str(r['plate_code'])))[:top_n_hot]}
    selected.update(ranked_reasons)
    seal_symbols = {r['symbol'] for r in seal_rows if r.get('side') in ('limit_up', 'limit_down')}
    selected.update(c for c, syms in members.items() if len(syms & seal_symbols) >= 2)
    taxonomy = build_taxonomy({c: members[c] for c in selected}, plate_names, boards)
    plates = taxonomy['plates']
    # Ask the existing calculator for every selected plate, including children.
    flat = dict(taxonomy, orphans=[c for c,p in plates.items() if not p['non_theme_reason']],
                plates={c: dict(p, is_broad=False) for c,p in plates.items()})
    computed = build_theme_cards(flat, auction_rows, hot_rows, seal_rows, listings=listings,
                                 trading_dates=trading_dates, trade_date=trade_date)
    rows = {x['plate_code']: x for x in computed['orphans'] + computed['non_theme']}
    current = {r['symbol']: r for r in auction_rows if r.get('auction_tag', '0925') == '0925'}
    previous = {r['symbol']: r for r in auction_rows if r.get('auction_tag') == '0924'}
    cap_rows = caps.get('rows', {}) if isinstance(caps, dict) and 'rows' in caps else (caps or {})
    for code, card in rows.items():
        n = card['auction_count']
        for label in ('strong', 'middle', 'weak'):
            card[label+'_share'] = card[label+'_count']/n if n else None
        h = hot.get(code, {})
        card.update(change_pct=h.get('change_pct'), kaipan_amount_yuan=h.get('amount_yuan'),
                    previous_limit_reason_count=len(reasons[code]))
        priced = [current[s] for s in members[code] if s in current and current[s].get('price_milli') is not None]
        deltas = [r['amount_yuan']-previous[r['symbol']]['amount_yuan'] for r in priced
                  if r.get('amount_yuan') is not None and r['symbol'] in previous and previous[r['symbol']].get('amount_yuan') is not None]
        card['amount_delta_yuan'] = sum(deltas) if priced and len(deltas)==len(priced) else None
        candidates = [(v.get('float_cap_yuan'), s) for s in members[code] if isinstance((v := cap_rows.get(s)), dict) and isinstance(v.get('float_cap_yuan'), (int,float)) and v['float_cap_yuan'] > 0]
        anchors = []
        for value, symbol in sorted(candidates, key=lambda x: (-x[0], x[1]))[:3]:
            q = current.get(symbol, {})
            anchors.append({'symbol': symbol, 'float_cap_yuan': value, 'change_bp': q.get('change_bp'), 'amount_yuan': q.get('amount_yuan')})
        card['anchors_status'] = 'available' if candidates else 'caps_unavailable'
        card['anchors'] = anchors
        values = [a['change_bp'] for a in anchors if a['change_bp'] is not None]
        card['anchor_delta_bp'] = mean(values)-card['mean_change_bp'] if values and card['mean_change_bp'] is not None else None
    valid = {c for c,p in plates.items() if not p['non_theme_reason']}
    roots = []
    for code in sorted(valid):
        card = rows[code]
        card['children'] = []
    for code in sorted(valid):
        parent = plates[code]['primary_parent']
        # Broad cards and unattached plates remain visible as cards.
        if parent in valid and plates[parent]['is_broad'] and not plates[code]['is_broad']:
            rows[parent]['children'].append(rows[code])
        else:
            roots.append(rows[code])
    excluded = [rows[c] for c in sorted(plates) if plates[c]['non_theme_reason']]
    return {'status':'available', 'cards':roots, 'plates':[rows[c] for c in sorted(valid)], 'excluded':excluded,
            'notes':['Stocks may appear in multiple themes; totals overlap.',
                     'excluded_non_theme=' + ','.join(f"{r['plate_code']}:{r['non_theme_reason']}" for r in excluded)],
            'taxonomy':taxonomy}


def read_and_build_theme_view(trade_date, redis_client, top_n_hot=40):
    """Read available facts from Redis; fail closed if prerequisites cannot be established."""
    import json
    from datetime import date, datetime
    from .data_pipeline.auction_redis_view import read_auction_view
    from .data_pipeline.redis_day_view_read import read_day_view
    stage = 'trade_date'
    try:
        day = date.fromisoformat(trade_date).isoformat()
        cutoff = datetime.fromisoformat(day+'T09:26:00+08:00')
        stage = 'hot_plates'
        versions = []
        for key in redis_client.scan_iter(match=f'core:cache:data:hot_plates:{day}:*', count=100):
            value = json.loads(redis_client.get(key))
            stamp = datetime.fromisoformat(value['available_at'].replace('Z','+00:00'))
            if stamp.tzinfo is not None and stamp <= cutoff and value.get('trade_date') == day and value.get('dataset') == 'hot_plates' and isinstance(value.get('rows'),list) and value.get('row_count') == len(value['rows']):
                versions.append((stamp,str(value.get('version','')),value))
        if not versions:
            raise ValueError('no_visible_hot_version')
        stage = 'auction_view'
        current = read_auction_view(redis_client, day, '0925')
        previous = read_auction_view(redis_client, day, '0924')
        stage = 'mapping'
        symbols = [r['symbol'] for r in current]
        mapping = read_day_view(redis_client, 'plate_stock_index', day, symbols, td_loader=lambda *_: {}, ledger=lambda *_: None)
        if mapping['source'] != 'redis' or len(mapping['rows']) != len(symbols):
            raise ValueError('mapping_incomplete')
        stage = 'trade_calendar'
        raise ValueError('redis_calendar_and_listing_facts_unavailable')
    except Exception as exc:
        return {'status':'unavailable', 'reason':f'{stage}:{type(exc).__name__}:{exc}'}


def render_theme_cards_html(view):
    def cell(value):
        return escape(str(value)) if value is not None else '—'
    def table(cards):
        return '<table><thead><tr><th>板块</th><th>竞价只数</th><th>强/中/弱</th><th>均值(bp)</th></tr></thead><tbody>' + ''.join(
            '<tr><td>'+cell(c['name'] or c['plate_code'])+'</td><td>'+cell(c['auction_count'])+'</td><td>'+cell(c['strong_count'])+'/'+cell(c['middle_count'])+'/'+cell(c['weak_count'])+'</td><td>'+cell(c['mean_change_bp'])+'</td></tr>' for c in cards) + '</tbody></table>'
    if view.get('status') != 'available':
        return '<p>数据不可用：'+cell(view.get('reason'))+'</p>'
    parts = ['<section><h2>今日竞价强弱</h2>', table(view['cards'])]
    for card in view['cards']:
        if card['children']:
            parts.append('<details><summary>'+cell(card['name'] or card['plate_code'])+'细分</summary>'+table(card['children'])+'</details>')
    parts.append('<details><summary>非题材</summary>'+table(view['excluded'])+'</details></section>')
    return ''.join(parts)
