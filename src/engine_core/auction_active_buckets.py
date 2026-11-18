"""Describe the plate memberships of stocks active in the auction."""
from collections import defaultdict
from html import escape
from statistics import mean

from .auction_theme_cards import non_theme_reason
from .data_pipeline.auction_seal_facts import classify_limit

BUCKET_VERSION = 'bucket_v2'
SMOOTH_K = 20


def build_active_bucket_view(auction_rows, stock_plates, yesterday_limit_up_symbols,
                             yesterday_reason_rows, *, names, top_amount=None,
                             top_abs_change=100, plate_names, hot_rows=None, caps=None,
                             trade_date=None, listings=None, trading_dates=None, sort_by='score',
                             rule='v2', min_abs_change_bp=500, min_amount_for_change=5000000,
                             sealed_symbols=None):
    if rule not in ('v1', 'v2'):
        raise ValueError('invalid_rule')
    if top_amount is None:
        top_amount = 100 if rule == 'v1' else 200
    if min_abs_change_bp < 0 or min_amount_for_change < 0:
        raise ValueError('invalid_change_threshold')
    if sort_by not in ('score', 'active_count'):
        raise ValueError('invalid_sort_by')
    for key, n in (('top_amount', top_amount), ('top_abs_change', top_abs_change)):
        if type(n) is not int or n < 0:
            raise ValueError('invalid_' + key)
    quotes = {r['symbol']: r for r in auction_rows if r.get('symbol')}
    priced = {s: r for s, r in quotes.items() if r.get('price_milli') is not None and r['price_milli'] > 0}
    sources = defaultdict(set)
    for symbol in yesterday_limit_up_symbols:
        sources[symbol].add('y')
    arms = [('a', 'amount_yuan', top_amount, False)]
    if rule == 'v1':
        arms.append(('c', 'change_bp', top_abs_change, True))
    for label, field, n, magnitude in arms:
        eligible = [r for r in priced.values() if r.get(field) is not None]
        eligible.sort(key=lambda r: (-(abs(r[field]) if magnitude else r[field]), r['symbol']))
        for r in eligible[:n]:
            sources[r['symbol']].add(label)
    if rule == 'v2':
        for r in priced.values():
            if (r.get('change_bp') is not None and abs(r['change_bp']) >= min_abs_change_bp
                    and r.get('amount_yuan') is not None and r['amount_yuan'] >= min_amount_for_change):
                sources[r['symbol']].add('c')
        for symbol in sealed_symbols or ():
            sources[symbol].add('s')
    reasons = defaultdict(set)
    for row in yesterday_reason_rows:
        symbol = row.get('symbol')
        if symbol not in sources or 'y' not in sources[symbol]:
            continue
        for code in [row.get('plate_code'), row.get('main_reason_plate_code'), *(row.get('plate_codes') or [])]:
            if code:
                reasons[symbol].add(str(code))
    members = defaultdict(set)
    stock_codes = {}
    for symbol, plates in stock_plates.items():
        codes = {str(p.get('plate_code') if isinstance(p, dict) else p) for p in plates if (p.get('plate_code') if isinstance(p, dict) else p) is not None}
        stock_codes[symbol] = sorted(codes)
        for code in codes:
            members[code].add(symbol)
    limit_available = trade_date is not None and listings is not None and trading_dates is not None
    limits = {s: classify_limit(trade_date, s, r, listings.get(s), trading_dates) for s, r in priced.items()} if limit_available else {}
    stocks = []
    for symbol in sorted(sources):
        q = priced.get(symbol, {})
        stocks.append(dict(symbol=symbol, name=names.get(symbol, symbol), plate_codes=stock_codes.get(symbol, []),
                           change_bp=q.get('change_bp'), amount_yuan=q.get('amount_yuan'),
                           sources=[x for x in ('yac' if rule == 'v1' else 'yacs') if x in sources[symbol]], price_missing=symbol not in priced,
                           yesterday_reason_plate_codes=sorted(reasons[symbol]), limit_status=limits.get(symbol)))
    by_symbol = {r['symbol']: r for r in stocks}
    amounts = [r.get('amount_yuan') for r in priced.values()]
    market_amount = sum(amounts) if all(v is not None for v in amounts) else None
    hot = {str(r['plate_code']): r for r in hot_rows or [] if r.get('plate_code') is not None}
    cap_rows = (caps.get('rows', caps) if caps else {})
    buckets = []
    for code, symbols in members.items():
        active = [by_symbol[s] for s in sorted(symbols & sources.keys())]
        if not active:
            continue
        vals = [r['amount_yuan'] for r in active]
        total = sum(vals) if all(v is not None for v in vals) else None
        changes = [r['change_bp'] for r in active if r['change_bp'] is not None]
        def cap(s):
            value = cap_rows.get(s)
            return value.get('float_cap_yuan') if isinstance(value, dict) else value
        if cap_rows:
            ranked = sorted((r for r in active if cap(r['symbol']) is not None), key=lambda r: (-cap(r['symbol']), r['symbol']))
            status = 'caps_ok' if ranked else 'caps_missing'
        else:
            ranked = sorted((r for r in active if r['amount_yuan'] is not None), key=lambda r: (-r['amount_yuan'], r['symbol']))
            status = 'caps_unavailable'
        name = plate_names.get(code) or hot.get(code, {}).get('name') or code
        bucket = dict(plate_code=code, name=name, non_theme_reason=non_theme_reason(name),
                      active_count=len(active), member_count=len(symbols), active_share=len(active)/len(symbols),
                      score=len(active)/(len(symbols)+SMOOTH_K),
                      mean_change_bp=mean(changes) if changes else None, amount_yuan=total,
                      amount_share=total/market_amount if total is not None and market_amount else None,
                      limit_up_count=sum(r['limit_status'] == 'limit_up' for r in active) if limit_available else None,
                      limit_down_count=sum(r['limit_status'] == 'limit_down' for r in active) if limit_available else None,
                      limit_unknown_count=sum(r['limit_status'] in ('unknown_listing', 'unknown_price') or r['price_missing'] for r in active) if limit_available else None,
                      leaders=[dict(symbol=r['symbol'], name=r['name'], change_bp=r['change_bp'],
                                    amount_yuan=r['amount_yuan'], float_cap_yuan=cap(r['symbol'])) for r in ranked[:3]], leaders_status=status,
                      yesterday_reason_hit_count=sum(code in reasons[r['symbol']] for r in active),
                      hot_rank=hot.get(code, {}).get('rank'), hot_change_pct=hot.get(code, {}).get('change_pct'))
        bucket.update({label + '_count': sum(label in r['sources'] for r in active) for label in 'yac'})
        if rule == 'v2':
            bucket['s_count'] = sum('s' in r['sources'] for r in active)
        buckets.append(bucket)
    buckets.sort(key=lambda r: ((-r['score'],) if sort_by == 'score' else ()) + (-r['active_count'], -(r['amount_yuan'] if r['amount_yuan'] is not None else -1), r['plate_code']))
    return dict(bucket_version='bucket_v1' if rule == 'v1' else BUCKET_VERSION, stocks=stocks, buckets=buckets, market_amount_yuan=market_amount,
                overlap_note='一股可能属于多个板块，板块汇总不可相加。')


def render_active_bucket_html(view):
    def cell(value):
        return escape('未知' if value is None else str(value))
    columns = (('板块', 'name'), ('活跃数 (昨/额/幅)', None), ('成员数', 'member_count'),
               ('活跃占比', 'active_share'), ('score', 'score'), ('活跃股平均涨幅', 'mean_change_bp'),
               ('涨停', 'limit_up_count'), ('跌停', 'limit_down_count'), ('竞价额占比', 'amount_share'),
               ('昨日涨停原因命中', 'yesterday_reason_hit_count'), ('热板rank', 'hot_rank'), ('龙头', None))
    parts = ['<h1>竞价强弱</h1><p>' + cell(view['overlap_note']) + '</p>', '<h2>板块</h2><table><thead><tr>',
             ''.join('<th>' + cell(label) + '</th>' for label, _ in columns), '</tr></thead><tbody>']
    for row in view['buckets']:
        name = cell(row['name'])
        if row['non_theme_reason']:
            name += ' <span style="color:gray">' + cell(row['non_theme_reason']) + '</span>'
        counts = f"{row['active_count']} ({row['y_count']}/{row['a_count']}/{row['c_count']})"
        leaders = '、'.join(cell(r['name']) + ' (' + (f"{r['change_bp']/100:+.2f}%" if r['change_bp'] is not None else '未知') + ')' for r in row['leaders'])
        values = [name, cell(counts)] + [cell(row.get(key)) for _, key in columns[2:-1]] + [leaders]
        parts.append('<tr>' + ''.join('<td>' + value + '</td>' for value in values) + '</tr>')
    fields = ('symbol', 'name', 'plate_codes', 'change_bp', 'amount_yuan', 'sources', 'price_missing', 'yesterday_reason_plate_codes')
    parts.append('</tbody></table><h2>个股</h2><table><thead><tr>' + ''.join('<th>' + cell(f) + '</th>' for f in fields) + '</tr></thead><tbody>')
    for row in view['stocks']:
        parts.append('<tr>' + ''.join('<td>' + cell(row.get(f)) + '</td>' for f in fields) + '</tr>')
    parts.append('</tbody></table>')
    return ''.join(parts)
