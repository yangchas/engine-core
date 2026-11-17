"""Build an independent, point-in-time auction theme preview from supplied facts."""
from collections import Counter, defaultdict
from html import escape
from statistics import mean

from .theme_taxonomy import build_theme_cards

STYLE_WORDS = ('可转债', '北交所', '破净', '高股息', '中字头', '超跌', '融资融券', '沪股通', '深股通', 'MSCI', '国有企业', '低价股', '股权转让')
FINANCIAL_WORDS = ('中报', '三季报', '年报', '业绩', '预增', '预亏', '增长')
PROVINCES = frozenset(('京', '津', '冀', '晋', '蒙', '辽', '吉', '黑', '沪', '苏', '浙', '皖', '闽', '赣', '鲁', '豫', '鄂', '湘', '粤', '桂', '琼', '渝', '川', '贵', '云', '藏', '陕', '甘', '青', '宁', '新'))


def non_theme_reason(name):
    name = name or ''
    if any(word.casefold() in name.casefold() for word in STYLE_WORDS):
        return 'style_name'
    if any(word in name for word in FINANCIAL_WORDS):
        return 'financial_name'
    if name in PROVINCES or name == '西部大开发' or any(word in name for word in ('省', '市', '自治区', '自贸区')):
        return 'geography_name'
    return None


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
    selected = sorted(hot_rows, key=lambda r: (r['rank'], str(r['plate_code'])))[:top_n_hot]
    plates = {}
    for h in selected:
        code = str(h['plate_code'])
        symbols = sorted(members[code])
        plates[code] = dict(plate_code=code, name=h.get('name') or plate_names.get(code),
                            members=symbols, member_count=len(symbols),
                            non_theme_reason=non_theme_reason(h.get('name') or plate_names.get(code)),
                            is_broad=False, primary_parent=None)
    flat = dict(parameters={}, plates=plates,
                orphans=[c for c, p in plates.items() if not p['non_theme_reason']])
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
                    previous_limit_reason_count=len(reasons[code]),
                    amount_to_kaipan_ratio=card['amount_yuan']/h['amount_yuan'] if card['amount_yuan'] is not None and h.get('amount_yuan') else None)
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
    ordered_codes = [str(h['plate_code']) for h in selected]
    valid = [c for c in ordered_codes if not plates[c]['non_theme_reason']]
    for code in valid:
        rows[code]['children'] = []
    excluded = [rows[c] for c in ordered_codes if plates[c]['non_theme_reason']]
    valid_set = set(valid)
    stock_hot = defaultdict(list)
    for code in valid:
        for symbol in members[code]:
            stock_hot[symbol].append(code)
    reason_by_stock = defaultdict(set)
    for symbol, row in pool.items():
        for code in [row.get('main_reason_plate_code'), row.get('plate_code')]:
            if code and str(code) in valid_set:
                reason_by_stock[symbol].add(str(code))
    for row in reason_rows:
        symbol = row.get('symbol')
        event_date = row.get('event_date') or row.get('date')
        if trade_date and event_date and event_date >= trade_date:
            continue
        for code in [row.get('main_reason_plate_code'), row.get('plate_code'), *(row.get('plate_codes') or [])]:
            if symbol and code and str(code) in valid_set:
                reason_by_stock[symbol].add(str(code))
    active = {r['symbol'] for r in seal_rows if r.get('side') in ('limit_up', 'limit_down') and r.get('symbol')}
    for field, descending in (('amount_yuan', True), ('change_bp', True), ('change_bp', False)):
        eligible = [r for r in current.values() if r.get(field) is not None]
        eligible.sort(key=lambda r: ((-r[field] if descending else r[field]), r['symbol']))
        active.update(r['symbol'] for r in eligible[:20])
    seals = {r['symbol']: r for r in seal_rows}
    rank_order = {code: i for i, code in enumerate(valid)}
    stocks = []
    for symbol in sorted(active):
        q, seal = current.get(symbol, {}), seals.get(symbol, {})
        choices = sorted(reason_by_stock[symbol], key=rank_order.get)
        fallback = stock_hot[symbol]
        code = choices[0] if choices else (fallback[0] if fallback else None)
        stocks.append(dict(symbol=symbol, name=q.get('name') or pool.get(symbol, {}).get('name') or seal.get('name') or (names or {}).get(symbol),
                           change_bp=q.get('change_bp'), amount_yuan=q.get('amount_yuan'),
                           seal_amount_yuan=seal.get('seal_amount_yuan'), price_source=seal.get('price_source') or q.get('src'),
                           side=seal.get('side'), primary_theme=({'plate_code':code, 'name':plates[code]['name']} if code else None),
                           theme_source='limit_reason' if choices else ('hot_membership' if code else 'UNKNOWN'),
                           hot_plates=[{'plate_code': c, 'name': plates[c]['name'], 'rank': rows[c]['rank']} for c in fallback[:5]]))
    cards = [rows[c] for c in valid]
    return {'status':'available', 'cards':cards, 'plates':cards, 'excluded':excluded, 'active_stocks':stocks,
            'notes':['Stocks may appear in multiple themes; totals overlap.',
                     'excluded_non_theme=' + ','.join(f"{r['rank']}:{r['plate_code']}:{r['name']}:{r['non_theme_reason']}" for r in excluded)]}



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
        columns = [('排名','rank'), ('板块','name'), ('竞价只数','auction_count'),
                   ('强只数','strong_count'), ('强占比','strong_share'), ('中只数','middle_count'),
                   ('中占比','middle_share'), ('弱只数','weak_count'), ('弱占比','weak_share'),
                   ('涨停','limit_up_count'), ('跌停','limit_down_count'), ('均值(bp)','mean_change_bp'),
                   ('加权均值(bp)','weighted_mean_change_bp'), ('去前三均值(bp)','mean_ex_top3_bp'),
                   ('金额前三占比','amount_top3_share'), ('竞价额','amount_yuan'),
                   ('全市场占比','market_amount_share'), ('金额增量','amount_delta_yuan'),
                   ('热板金额比','amount_to_kaipan_ratio'), ('热度','strength'), ('热板涨幅','change_pct'),
                   ('昨日涨停原因数','previous_limit_reason_count'), ('龙头状态','anchors_status')]
        head = ''.join('<th>'+cell(label)+'</th>' for label, _ in columns) + '<th>龙头</th>'
        body = ''.join('<tr>' + ''.join('<td>'+cell(c.get(key))+'</td>' for _, key in columns) +
                       '<td>'+cell('、'.join(a['symbol'] for a in c['anchors']))+'</td></tr>' for c in cards)
        return '<table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table>'
    if view.get('status') != 'available':
        return '<p>数据不可用：'+cell(view.get('reason'))+'</p>'
    parts = ['<section><h2>热板卡片</h2>', table(view['cards'])]
    parts.append('<h2>活跃个股</h2><table><thead><tr><th>代码</th><th>名称</th><th>涨跌(bp)</th><th>成交额</th><th>封单额</th><th>价格来源</th><th>主题材</th><th>证据</th><th>所属热板</th></tr></thead><tbody>')
    for stock in view['active_stocks']:
        values = [stock['symbol'], stock['name'], stock['change_bp'], stock['amount_yuan'], stock['seal_amount_yuan'], stock['price_source'], (stock['primary_theme'] or {}).get('name'), stock['theme_source'], '、'.join(p['name'] or p['plate_code'] for p in stock['hot_plates'])]
        parts.append('<tr>' + ''.join('<td>'+cell(v)+'</td>' for v in values) + '</tr>')
    parts.append('</tbody></table><details><summary>非题材（保留原排名）</summary><ul>')
    parts.extend('<li>'+cell(c['rank'])+' '+cell(c['name'])+' '+cell(c['non_theme_reason'])+'</li>' for c in view['excluded'])
    parts.append('</ul></details></section>')
    return ''.join(parts)
