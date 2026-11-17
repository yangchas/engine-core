"""Pure, partitioned attribution of active auction stocks to current themes."""
from collections import Counter, defaultdict
from html import escape
from fractions import Fraction
from math import log
from statistics import mean

from .auction_theme_cards import non_theme_reason

THEME_VERSION = 'theme_v2'
MIN_SPECIFIC_MEMBERS = 50


def assign_stock_themes(active_stocks, stock_plates, plate_names, plate_member_counts,
                        reason_history, *, today_active_support=None, max_history_days=20,
                        big_plate_members=800, secondary=2, caps=None, limit_available=False):
    """Attribute supplied stocks; reason_history must contain only pre-auction dates.

    History is a mapping from symbol to rows with ``date`` and ``plate_codes``.
    The 20-day horizon is based on dates supplied across the history, so callers
    should prefilter using their trading calendar before invoking this function.
    """
    if max_history_days < 1 or secondary < 0:
        raise ValueError('invalid attribution limits')
    def code(p):
        return str(p.get('plate_code') if isinstance(p, dict) else p)
    mapping = {s: {code(p) for p in plates if (p.get('plate_code') if isinstance(p, dict) else p) is not None} for s, plates in stock_plates.items()}
    population = sum(bool(v) for v in mapping.values())
    support = Counter(c for stock in active_stocks for c in mapping.get(stock['symbol'], ())) if today_active_support is None else today_active_support
    dates = sorted({str(r['date']) for rows in reason_history.values() for r in rows if r.get('date')}, reverse=True)[:max_history_days]
    date_set = set(dates)
    stocks, ignored = [], 0
    for stock in active_stocks:
        symbol = stock['symbol']
        current = mapping.get(symbol, set())
        hits = defaultdict(list)
        for row in reason_history.get(symbol, ()):
            if str(row.get('date')) not in date_set:
                continue
            for c in set(str(c) for c in row.get('plate_codes', ()) if c):
                if c in current:
                    hits[c].append(str(row['date']))
                else:
                    ignored += 1
        def support_count(c):
            return max(0, support.get(c, 0) - 1)
        def specificity(c):
            n = plate_member_counts.get(c)
            return log(population / n) if population and n and n > 0 else float('-inf')
        def key(c):
            if c in hits:
                # Date first, frequency second, then deterministic code.
                return (0, -int(max(hits[c]).replace('-', '')), -len(hits[c]), c)
            n = plate_member_counts.get(c)
            eligible = not non_theme_reason(plate_names.get(c) or c) and n is not None and 0 < n <= big_plate_members
            if eligible and n >= MIN_SPECIFIC_MEMBERS and support_count(c):
                return (1, -Fraction(support.get(c, 0), n + 20), -support.get(c, 0), c)
            return (2 if eligible else 3, -specificity(c), -support_count(c), c)
        ordered = sorted(current, key=key)
        choices = []
        for rank, c in enumerate(ordered[:1 + secondary], 1):
            tier = key(c)[0]
            source = ('reason', 'support', 'specific', 'fallback')[tier]
            if tier == 2 and plate_member_counts[c] < MIN_SPECIFIC_MEMBERS:
                source = 'fallback' if rank == 1 else 'specific_small'
            choices.append({'plate_code': c, 'name': plate_names.get(c) or c,
                            'source': source, 'rank': rank})
        stocks.append({**stock, 'primary': choices[0] if choices else None,
                       'secondary': choices[1:], 'status': 'assigned' if choices else 'no_plate'})
    groups = defaultdict(list)
    secondary_counts = Counter()
    for row in stocks:
        if row['primary']:
            groups[row['primary']['plate_code']].append(row)
        secondary_counts.update(x['plate_code'] for x in row['secondary'])
    cap_rows = (caps.get('rows', caps) if caps else {})
    def cap(symbol):
        v = cap_rows.get(symbol)
        return v.get('float_cap_yuan') if isinstance(v, dict) else v
    buckets = []
    for c, rows in groups.items():
        changes = sorted((r['change_bp'] for r in rows if r.get('change_bp') is not None), reverse=True)
        amounts = [r.get('amount_yuan') for r in rows]
        has_caps = any(cap(r['symbol']) is not None for r in rows)
        if has_caps:
            leaders = sorted((r for r in rows if cap(r['symbol']) is not None), key=lambda r: (-cap(r['symbol']), r['symbol']))
        else:
            leaders = sorted((r for r in rows if r.get('amount_yuan') is not None), key=lambda r: (-r['amount_yuan'], r['symbol']))
        statuses = [r.get('limit_status') for r in rows]
        buckets.append({'plate_code': c, 'name': plate_names.get(c) or c, 'active_count': len(rows),
                        'yesterday_count': sum('y' in r.get('sources', ()) for r in rows),
                        'mean_change_bp': mean(changes) if changes else None,
                        'mean_ex_top1_bp': mean(changes[1:]) if len(changes) > 1 else None,
                        'mean_ex_top3_bp': mean(changes[3:]) if len(changes) > 3 else None,
                        'amount_yuan': sum(amounts) if all(x is not None for x in amounts) else None,
                        'limit_up_count': sum(x == 'limit_up' for x in statuses) if limit_available and not any(x in ('unknown_listing', 'unknown_price') or r.get('price_missing') for x, r in zip(statuses, rows)) else None,
                        'limit_down_count': sum(x == 'limit_down' for x in statuses) if limit_available and not any(x in ('unknown_listing', 'unknown_price') or r.get('price_missing') for x, r in zip(statuses, rows)) else None,
                        'leaders': [{'symbol': r['symbol'], 'name': r.get('name') or r['symbol']} for r in leaders[:3]],
                        'leaders_status': 'float_cap' if has_caps else 'amount_fallback',
                        'member_count': plate_member_counts.get(c), 'secondary_count': secondary_counts[c]})
    buckets.sort(key=lambda r: (-r['active_count'], -(r['amount_yuan'] if r['amount_yuan'] is not None else -1), r['plate_code']))
    return {'theme_version': THEME_VERSION, 'stocks': stocks, 'buckets': buckets, 'ignored_reason_count': ignored}


def render_stock_themes_html(view):
    def cell(v):
        return escape('未知' if v is None else str(v))
    def table(headers, rows):
        return '<table><thead><tr>' + ''.join('<th>' + cell(x) + '</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + cell(x) + '</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table>'
    fields = ('name', 'active_count', 'yesterday_count', 'mean_change_bp', 'amount_yuan', 'limit_up_count', 'limit_down_count', 'member_count', 'secondary_count', 'mean_ex_top1_bp', 'mean_ex_top3_bp')
    bucket_rows = ([*(r.get(k) for k in fields), '、'.join(x['name'] for x in r['leaders']), r['leaders_status']] for r in view['buckets'])
    stock_rows = ([r.get('name'), r.get('symbol'), r.get('change_bp'), (r['primary'] or {}).get('name'), '、'.join(x['name'] + '(' + x['source'] + ')' for x in r['secondary']), (r['primary'] or {}).get('source', 'no_plate')] for r in view['stocks'])
    return '<h1>竞价强弱</h1><h2>桶表</h2>' + table(('板块','主题材活跃只数','昨日涨停只数','平均涨幅bp','竞价额','涨停','跌停','成员数','次题材命中','去最高1均值bp','去前3均值bp','龙头','龙头来源'), bucket_rows) + '<h2>个股表</h2>' + table(('名称','代码','涨幅bp','主题材','次题材','来源'), stock_rows)
