"""Fact-only summaries of a prepared auction theme and amount board."""


def _relay(stocks, limit_available):
    rows = [s for s in stocks if 'y' in s.get('sources', ())]
    changes = [s['change_bp'] for s in rows if s.get('change_bp') is not None and not s.get('price_missing')]
    statuses = [s.get('limit_status') for s in rows]
    known = limit_available and all(x not in ('unknown_listing', 'unknown_price') and not s.get('price_missing') for x, s in zip(statuses, rows))
    return {'count': len(rows), 'priced': len(changes), 'high': sum(x > 0 for x in changes),
            'flat': sum(x == 0 for x in changes), 'low': sum(x < 0 for x in changes),
            'limit_up': sum(x == 'limit_up' for x in statuses) if known else None,
            'limit_down': sum(x == 'limit_down' for x in statuses) if known else None}


def build_anomaly_digest(board_view, topk=5, min_effective=3, relay_min_yesterday=2):
    if min(topk, min_effective, relay_min_yesterday) < 0:
        raise ValueError('invalid_digest_limit')
    buckets = board_view['buckets']
    eligible = [b for b in buckets if b['active']['count'] >= min_effective
                and b['active']['relative_ratio'] is not None]
    eligible.sort(key=lambda b: (-b['active']['relative_ratio'], b['plate_code']))
    strong = [b for b in eligible if b['active']['label'] == '放量偏强'][:topk]
    weak = [b for b in eligible if b['active']['label'] == '放量偏弱'][:topk]
    stocks = board_view['stocks']
    limit_available = any(b.get('limit_up_count') is not None for b in buckets)
    groups = []
    for b in buckets:
        rows = [s for s in stocks if (s.get('primary') or {}).get('plate_code') == b['plate_code']]
        relay = _relay(rows, limit_available)
        if relay['count'] >= relay_min_yesterday:
            groups.append({'name': b['name'], 'plate_code': b['plate_code'], **relay})
    groups.sort(key=lambda x: (-x['count'], x['plate_code']))
    return {'market': board_view['market'], 'strong': strong, 'weak': weak,
            'relay_market': _relay(stocks, limit_available), 'relay_themes': groups[:8],
            'drift': [b for b in strong + weak if b.get('drift_flag')]}


def render_digest_md(digest):
    def fmt(v, scale=1):
        return '未知' if v is None else f'{v / scale:.2f}'
    def label(v):
        return '未知' if v is None else '放量' if v >= 1.2 else '缩量' if v <= 0.8 else '平量'
    market = digest['market']
    today = market['today_yuan']
    history = '、'.join(f"{d} {fmt(v, 1e8)}亿 比值 {fmt(today / v if today is not None and v else None)}（{label(today / v if today is not None and v else None)}）" for d, v in sorted(market['prior_yuan'].items()))
    lines = [f"市场：今日竞价额 {fmt(today, 1e8)}亿；前几日 {history}；market_ratio {fmt(market['ratio'])}（{label(market['ratio'])}）"]
    for title, key in (('放量偏强', 'strong'), ('放量偏弱', 'weak')):
        lines.append(f'\n{title}：')
        for b in digest[key]:
            leaders = '、'.join(f"{x['name']} {fmt(x.get('change_bp'), 100)}%" if x.get('change_bp') is not None else f"{x['name']} 未知" for x in b['leaders']) or '未知'
            drift = '偏移：' + '、'.join(b['drift_reasons']) if b.get('drift_flag') else '无偏移'
            lines.append(f"- {b['name']}：相对放量 {fmt(b['active']['relative_ratio'])}，有效 {b['active']['count']}只，{drift}；龙头 {leaders}")
        if not digest[key]:
            lines.append('- 无')
    def relay_line(name, r):
        val = lambda x: '未知' if x is None else str(x)
        return f"{name}：昨涨停 {r['count']}只；今日高开 {r['high']}、平 {r['flat']}、低开 {r['low']}只（有价 {r['priced']}只）；今日涨停 {val(r['limit_up'])}、跌停 {val(r['limit_down'])}只"
    lines.append('\n昨日涨停接力：')
    lines.append(relay_line('全市场', digest['relay_market']))
    lines.extend('- ' + relay_line(r['name'], r) for r in digest['relay_themes'])
    lines.append('\n偏移提示：')
    lines.extend('- ' + b['name'] + '：' + '、'.join(b['drift_reasons']) for b in digest['drift'])
    if not digest['drift']:
        lines.append('- 无')
    return '\n'.join(lines) + '\n'


def render_digest_with_appendix_md(digest, board_md):
    return render_digest_md(digest) + '\n## 附录\n\n' + board_md
