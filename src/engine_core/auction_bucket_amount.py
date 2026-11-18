"""Fixed-membership auction amount comparisons; no data access or side effects."""
from html import escape
from statistics import mean

AMOUNT_CONTEXT_VERSION = 'amount_v1'
MIN_RANK_STOCKS = 3


def amount_status(relative_ratio, r_bp, *, surge=1.2, shrink=0.8, strong_bp=50):
    if relative_ratio is None or r_bp is None:
        return None
    volume = '放量' if relative_ratio >= surge else '缩量' if relative_ratio <= shrink else '平量'
    strength = '偏强' if r_bp >= strong_bp else '偏弱' if r_bp <= -strong_bp else '中性'
    return volume + strength


def add_bucket_amount_context(theme_view, today_rows, prior_rows_by_date, stock_plates, *, min_days=2, surge=1.2, shrink=0.8, strong_bp=50):
    if min_days < 1:
        raise ValueError('min_days must be positive')
    dates = sorted(prior_rows_by_date, reverse=True)[:4]
    def amounts(rows):
        return {r['symbol']: r['amount_yuan'] for r in rows if r.get('amount_yuan') is not None}
    today = amounts(today_rows)
    previous = {d: amounts(prior_rows_by_date[d]) for d in dates}
    common = set(today)
    for values in previous.values():
        common.intersection_update(values)
    def calculate(members):
        symbols = common.intersection(members)
        current = sum(today[s] for s in symbols) if symbols else None
        totals = {d: sum(previous[d][s] for s in symbols) if symbols else None for d in dates}
        avg = mean(totals.values()) if totals and symbols else None
        status = 'days_insufficient' if len(dates) < min_days else 'no_common_members' if not symbols else 'zero_baseline' if not avg else 'ok'
        return {'today_yuan': current, 'prior_yuan': totals, 'prior_mean_yuan': avg,
                'ratio': current / avg if status == 'ok' else None, 'count': len(symbols), 'status': status}
    market = calculate(set(today))
    changes = [r['change_bp'] for r in today_rows if r.get('change_bp') is not None]
    market_change = mean(changes) if changes else None
    members = {}
    for symbol, plates in stock_plates.items():
        for plate in plates:
            code = plate.get('plate_code') if isinstance(plate, dict) else plate
            if code is not None:
                members.setdefault(str(code), set()).add(symbol)
    active = {}
    for stock in theme_view['stocks']:
        primary = stock.get('primary')
        if primary and primary.get('plate_code') is not None:
            active.setdefault(str(primary['plate_code']), set()).add(stock['symbol'])
    buckets = []
    for bucket in theme_view['buckets']:
        code = str(bucket['plate_code'])
        delta = bucket.get('mean_change_bp')
        delta = delta - market_change if delta is not None and market_change is not None else None
        enriched = {**bucket, 'r_bp': delta}
        for kind, population in (('active', active), ('plate', members)):
            result = calculate(population.get(code, set()))
            ratio = result['ratio']
            relative = ratio / market['ratio'] if ratio is not None and market['ratio'] else None
            result['relative_ratio'] = relative
            result['label'] = amount_status(relative, delta, surge=surge, shrink=shrink, strong_bp=strong_bp)
            enriched[kind] = result
        buckets.append(enriched)
    return {**theme_view, 'amount_context_version': AMOUNT_CONTEXT_VERSION, 'market': market,
            'market_mean_change_bp': market_change, 'buckets': buckets}


def render_bucket_amount_html(view):
    def fmt(v, scale=1):
        return '未知' if v is None else f'{v / scale:.2f}'
    sections = ['<h1>竞价量价</h1>']
    for kind, name in (('active', '主题材活跃股'), ('plate', '板块全部成员')):
        sections.append('<h2>' + name + '</h2><table><thead><tr>' + ''.join('<th>'+x+'</th>' for x in ('板块','今日竞价额(亿)','前几日均值(亿)','放量倍数','相对放量','r_bp','状态')) + '</tr></thead><tbody>')
        for bucket in view['buckets']:
            row = bucket[kind]
            values = (bucket['name'], fmt(row['today_yuan'], 1e8), fmt(row['prior_mean_yuan'], 1e8), fmt(row['ratio']), fmt(row['relative_ratio']), fmt(bucket['r_bp']), row['label'] or '未知')
            sections.append('<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in values) + '</tr>')
        sections.append('</tbody></table>')
    return ''.join(sections)


def rank_relative_amount(view, kind='active'):
    """Rank only buckets with enough comparable stocks and a valid ratio."""
    eligible = (b for b in view['buckets'] if b[kind]['count'] >= MIN_RANK_STOCKS and b[kind]['relative_ratio'] is not None)
    return sorted(eligible, key=lambda b: (-b[kind]['relative_ratio'], b['plate_code']))


def render_bucket_board_md(view):
    def fmt(value, scale=1, suffix=''):
        return '未知' if value is None else f'{value / scale:.2f}{suffix}'
    def label(ratio):
        return '未知' if ratio is None else '放量' if ratio >= 1.2 else '缩量' if ratio <= 0.8 else '平量'
    market = view['market']
    history = '、'.join(f"{d} {fmt(v, 1e8)}亿 比值 {fmt(market['today_yuan'] / v if market['today_yuan'] is not None and v else None)}（{label(market['today_yuan'] / v if market['today_yuan'] is not None and v else None)}）" for d, v in sorted(market['prior_yuan'].items()))
    lines = [f"市场：今日竞价额 {fmt(market['today_yuan'], 1e8)}亿；前几日 {history}；market_ratio {fmt(market['ratio'])}（{label(market['ratio'])}）", '',
             '| 主题材 | 活跃只数(仅主/主加次) | 有效只数 | 昨涨停 | 今日竞价额(亿) | 前几日均值(亿) | 放量倍数 | 相对放量 | 状态 | 平均涨幅(仅主/主加次)% | 去掉最高1只后% | 涨停/跌停 | 偏移 | 龙头 |',
             '| ' + ' | '.join(['---'] * 14) + ' |']
    for b in view['buckets']:
        a = b['active']
        leaders = '、'.join(x['name'] + ' ' + fmt(x.get('change_bp'), 100, '%') for x in b['leaders'])
        values = (b['name'], f"{b['active_count']}/{b['pps_count']}", str(a['count']), str(b['yesterday_count']), fmt(a['today_yuan'], 1e8), fmt(a['prior_mean_yuan'], 1e8), fmt(a['ratio']), fmt(a['relative_ratio']), a['label'] or '未知', fmt(b['mean_change_bp'], 100, '%') + '/' + fmt(b['pps_mean_change_bp'], 100, '%'), fmt(b['mean_ex_top1_bp'], 100, '%'), f"{b['limit_up_count'] if b['limit_up_count'] is not None else '未知'}/{b['limit_down_count'] if b['limit_down_count'] is not None else '未知'}", '、'.join(b['drift_reasons']) or '—', leaders)
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
    return '\n'.join(lines) + '\n'
