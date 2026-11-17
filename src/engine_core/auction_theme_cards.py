"""Build an independent, point-in-time auction theme preview from supplied facts."""
from collections import Counter, defaultdict
from html import escape
from datetime import datetime
from statistics import mean
from math import comb

from .theme_taxonomy import build_theme_cards

STYLE_WORDS = ('可转债', '北交所', '破净', '高股息', '中字头', '超跌', '融资融券', '沪股通', '深股通', 'MSCI', '国有企业', '低价股', '股权转让')
FINANCIAL_WORDS = ('中报', '三季报', '年报', '业绩', '预增', '预亏', '增长')
PROVINCES = frozenset(('京', '津', '冀', '晋', '蒙', '辽', '吉', '黑', '沪', '苏', '浙', '皖', '闽', '赣', '鲁', '豫', '鄂', '湘', '粤', '桂', '琼', '渝', '川', '贵', '云', '藏', '陕', '甘', '青', '宁', '新'))
PROVINCE_NAMES = frozenset(('北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'))


def non_theme_reason(name):
    name = name or ''
    if any(word.casefold() in name.casefold() for word in STYLE_WORDS):
        return 'style_name'
    if any(word in name for word in FINANCIAL_WORDS):
        return 'financial_name'
    geography_suffix = name.endswith(('省', '自治区', '自贸区', '特别行政区'))
    three_char_city = len(name) == 3 and name.endswith('市')
    if name in PROVINCES or name in PROVINCE_NAMES or name == '西部大开发' or geography_suffix or three_char_city:
        return 'geography_name'
    return None


def _hypergeom_tail(population, successes, draws, hits):
    if not population or draws > population or successes > population:
        return None
    lo = max(hits, 0, draws - (population - successes))
    hi = min(draws, successes)
    return sum(comb(successes, k) * comb(population - successes, draws - k)
               for k in range(lo, hi + 1)) / comb(population, draws)


def build_auction_theme_view(auction_rows, stock_plates, hot_rows, seal_rows, pool_rows, reason_rows, *,
                             names=None, boards=None, listings=None, trading_dates=None, trade_date=None,
                             previous_trade_date=None, caps=None, top_n_hot=40, bottom_n_hot=40, k_active=2,
                             size_cap=400, display_top=50, active_top_n=20, min_active=2, sort_by='p_value', group_jaccard=None, group_contain=None, alpha=0.05):
    for key, value in (('top_n_hot', top_n_hot), ('bottom_n_hot', bottom_n_hot), ('k_active', k_active), ('size_cap', size_cap), ('display_top', display_top), ('active_top_n', active_top_n), ('min_active', min_active)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('invalid_' + key)
    # group_jaccard is a deprecated alias for the leader containment threshold.
    if group_jaccard is not None and (not isinstance(group_jaccard, (int, float)) or isinstance(group_jaccard, bool) or not 0 <= group_jaccard <= 1):
        raise ValueError('invalid_group_jaccard')
    if group_contain is None:
        group_contain = group_jaccard if group_jaccard is not None else 0.5
    for key, value in (('group_contain', group_contain), ('alpha', alpha)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise ValueError('invalid_' + key)
    if sort_by not in ('p_value', 'lift', 'active_count'):
        raise ValueError('invalid_sort_by')
    if not stock_plates:
        return {'status': 'unavailable', 'reason': 'mapping_unavailable'}
    members = defaultdict(set)
    for symbol, plates in stock_plates.items():
        for plate in plates:
            members[str(plate.get('plate_code') if isinstance(plate, dict) else plate)].add(symbol)
    if trade_date:
        cutoff = datetime.fromisoformat(trade_date + 'T09:26:00+08:00')
        hot_rows = [row for row in hot_rows if not row.get('available_at') or
                    (datetime.fromisoformat(row['available_at'].replace('Z', '+00:00')).tzinfo is not None and
                     datetime.fromisoformat(row['available_at'].replace('Z', '+00:00')) <= cutoff)]
    hot = {str(row['plate_code']): row for row in hot_rows}
    plate_names = dict(names or {})
    plate_names.update({c: r['name'] for c, r in hot.items() if r.get('name')})
    pool = {r['symbol']: r for r in pool_rows if r.get('symbol')}
    reasons = defaultdict(set)
    for symbol, row in pool.items():
        for code in (row.get('main_reason_plate_code'), row.get('plate_code')):
            if code:
                reasons[str(code)].add(symbol)
    for row in reason_rows:
        symbol = row.get('symbol')
        if symbol not in pool or (previous_trade_date and (row.get('event_date') or row.get('date')) != previous_trade_date):
            continue
        for code in (row.get('main_reason_plate_code'), row.get('plate_code')):
            if code:
                reasons[str(code)].add(symbol)
    current = {r['symbol']: r for r in auction_rows if r.get('auction_tag', '0925') == '0925'}
    active = {r['symbol'] for r in seal_rows if r.get('side') in ('limit_up', 'limit_down') and r.get('symbol')}
    for field, descending in (('amount_yuan', True), ('change_bp', True), ('change_bp', False)):
        eligible = [r for r in current.values() if r.get(field) is not None]
        eligible.sort(key=lambda r: ((-r[field] if descending else r[field]), r['symbol']))
        active.update(r['symbol'] for r in eligible[:active_top_n])
    auction_active = active & current.keys()
    active_counts = {code: len(symbols & active) for code, symbols in members.items()}
    ranked = sorted(hot_rows, key=lambda r: (r['rank'], str(r['plate_code'])))
    selected_codes = {str(h['plate_code']) for h in ranked[:top_n_hot]}
    if bottom_n_hot:
        selected_codes.update(str(h['plate_code']) for h in ranked[-bottom_n_hot:])
    selected_codes.update(code for code, symbols in members.items()
                          if plate_names.get(code) and len(symbols) <= size_cap
                          and active_counts[code] >= k_active)
    def rank_key(code):
        rank = hot.get(code, {}).get('rank')
        return (rank if rank is not None else float('inf'), code)
    ordered_codes = sorted(selected_codes, key=rank_key)
    plates = {}
    for code in ordered_codes:
        symbols = sorted(members[code])
        name = plate_names.get(code)
        plates[code] = dict(plate_code=code, name=name, members=symbols,
                            member_count=len(symbols), non_theme_reason=non_theme_reason(name),
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
            anchors.append({'symbol': symbol, 'name': q.get('name') or (names or {}).get(symbol), 'float_cap_yuan': value, 'change_bp': q.get('change_bp'), 'amount_yuan': q.get('amount_yuan')})
        card['anchors_status'] = 'available' if candidates else 'caps_unavailable'
        card['anchors'] = anchors
        values = [a['change_bp'] for a in anchors if a['change_bp'] is not None]
        card['anchor_delta_bp'] = mean(values)-card['mean_change_bp'] if values and card['mean_change_bp'] is not None else None
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
    for code, symbols in reasons.items():
        if code in valid_set:
            for symbol in symbols & members[code]:
                reason_by_stock[symbol].add(code)
    seals = {r['symbol']: r for r in seal_rows}
    def choice_key(code):
        return (-active_counts[code], len(members[code]), *rank_key(code))
    cap_positions = {}
    for c in valid:
        eligible_caps = sorted(((cap_rows[s]['float_cap_yuan'], s) for s in members[c] & current.keys()
                                if isinstance(cap_rows.get(s), dict)
                                and isinstance(cap_rows[s].get('float_cap_yuan'), (int, float))
                                and cap_rows[s]['float_cap_yuan'] > 0), key=lambda x: (-x[0], x[1]))
        total_cap = sum(v for v, _ in eligible_caps)
        for rank, (value, s) in enumerate(eligible_caps, 1):
            cap_positions[c, s] = (rank, value / total_cap)
    stocks = []
    for symbol in sorted(active):
        q, seal = current.get(symbol, {}), seals.get(symbol, {})
        choices = sorted(reason_by_stock[symbol], key=choice_key)
        fallback = sorted(stock_hot[symbol], key=choice_key)
        eligible = [c for c in fallback if len(members[c]) <= size_cap]
        cap_choices = [c for c in fallback if len(members[c] & current.keys()) >= 30
                       and (c, symbol) in cap_positions and cap_positions[c, symbol][0] <= 3]
        cap_choices.sort(key=lambda c: (-cap_positions[c, symbol][1], *choice_key(c)))
        anchor_themes = [dict(plate_code=c, name=plates[c]['name'], cap_rank=cap_positions[c, symbol][0],
                              cap_share=cap_positions[c, symbol][1]) for c in cap_choices[:3]]
        code = choices[0] if choices else (eligible[0] if eligible else None)
        cap_info = cap_positions.get((code, symbol))
        stocks.append(dict(symbol=symbol, name=q.get('name') or pool.get(symbol, {}).get('name') or seal.get('name') or (names or {}).get(symbol),
                           change_bp=q.get('change_bp'), amount_yuan=q.get('amount_yuan'),
                           float_cap_yuan=cap_rows.get(symbol, {}).get('float_cap_yuan'),
                           anchor_themes=anchor_themes, anchor_cap_rank=cap_info[0] if cap_info else None,
                           anchor_cap_share=cap_info[1] if cap_info else None,
                           seal_amount_yuan=seal.get('seal_amount_yuan'), price_source=seal.get('price_source') or q.get('src'),
                           side=seal.get('side'), primary_theme=({'plate_code':code, 'name':plates[code]['name']} if code else None),
                           theme_source='limit_reason' if choices else ('active_membership' if code else 'UNKNOWN'),
                           hot_plates=[{'plate_code': c, 'name': plates[c]['name'], 'rank': rows[c]['rank']} for c in fallback[:5]]))
    all_cards = [rows[c] for c in valid]
    population, successes = len(current), len(auction_active)
    directional = {direction: {s for s in auction_active if current[s].get('change_bp') is not None and (current[s]['change_bp'] > 0 if direction == 'up' else current[s]['change_bp'] < 0)} for direction in ('up', 'down')}
    for card in all_cards:
        card_members = members[card['plate_code']] & current.keys()
        card['active_count'] = len(card_members & auction_active)
        expected = len(card_members) * successes / population if population else None
        card['expected_active'] = expected
        card['lift'] = card['active_count'] / expected if expected else None
        card['p_value'] = _hypergeom_tail(population, successes, len(card_members), card['active_count'])
        for direction, symbols in directional.items():
            count = len(card_members & symbols)
            expected_direction = len(card_members) * len(symbols) / population if population else None
            card['active_' + direction + '_count'] = count
            card['p_' + direction] = _hypergeom_tail(population, len(symbols), len(card_members), count)
            card['expected_' + direction] = expected_direction
            card['lift_' + direction] = count / expected_direction if expected_direction else None
    cards = [c for c in all_cards if c['active_count'] >= min_active]
    def sort_key(c):
        amount = (c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code'])
        if sort_by == 'active_count':
            return (-c['active_count'], *amount)
        lift_key = (c['lift'] is None, -(c['lift'] or 0), -c['active_count'], *amount)
        return (c['p_value'] is None, c['p_value'] if c['p_value'] is not None else 0, *lift_key) if sort_by == 'p_value' else lift_key
    cards.sort(key=sort_key)
    def directional_key(card, direction):
        p = card['p_' + direction]
        lift = card['lift_' + direction]
        return (p is None, p if p is not None else 0, lift is None, -(lift or 0),
                -card['active_' + direction + '_count'], card['amount_yuan'] is None,
                -(card['amount_yuan'] or 0), card['plate_code'])
    insignificant_cards = [c for c in cards if c['p_value'] is not None and c['p_value'] > alpha]
    cards = [c for c in cards if c['p_value'] is not None and c['p_value'] <= alpha]
    directional_ranked = {d: sorted((c for c in all_cards if c['active_' + d + '_count'] >= min_active),
                                    key=lambda c: directional_key(c, d)) for d in ('up', 'down')}
    insignificant = {d: [c for c in directional_ranked[d] if c['p_' + d] is not None and c['p_' + d] > alpha] for d in ('up', 'down')}
    up_cards, down_cards = ([c for c in directional_ranked[d] if c['p_' + d] is not None and c['p_' + d] <= alpha][:display_top] for d in ('up', 'down'))
    def group_cards(ranked_cards, active_symbols, p_key):
        def key(c):
            p = c[p_key]
            return (p is None, p if p is not None else 0, c['plate_code'])
        groups = []
        for card in sorted(ranked_cards, key=key):
            own = members[card['plate_code']] & active_symbols
            choices = []
            for group in groups:
                head = group['head']
                overlap = len(own & members[head['plate_code']]) / len(own) if own else 0
                if own and overlap >= group_contain:
                    choices.append((-overlap, key(head), group))
            if choices:
                min(choices, key=lambda x: (x[0], x[1]))[2]['others'].append(card)
            else:
                groups.append({'head': card, 'others': []})
        return groups
    groups = group_cards(cards[:display_top], auction_active, 'p_value')
    up_groups = group_cards(up_cards, directional['up'], 'p_up')
    down_groups = group_cards(down_cards, directional['down'], 'p_down')
    return {'status':'available', 'insignificant_cards':insignificant_cards, 'up_insignificant_cards':insignificant['up'], 'down_insignificant_cards':insignificant['down'], 'up_cards':up_cards, 'down_cards':down_cards, 'groups':groups, 'up_groups':up_groups, 'down_groups':down_groups, 'cards':cards, 'plates':all_cards, 'display_cards':cards[:display_top], 'more_cards':cards[display_top:], 'excluded':excluded, 'active_stocks':stocks,
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
        columns = [('排名','rank'), ('板块','name'), ('活跃股数','active_count'), ('期望数','expected_active'), ('倍数','lift'), ('p 值','p_value'), ('竞价只数','auction_count'),
                   ('强只数','strong_count'), ('强占比','strong_share'), ('中只数','middle_count'),
                   ('中占比','middle_share'), ('弱只数','weak_count'), ('弱占比','weak_share'),
                   ('涨停','limit_up_count'), ('跌停','limit_down_count'), ('均值(bp)','mean_change_bp'),
                   ('加权均值(bp)','weighted_mean_change_bp'), ('去前三均值(bp)','mean_ex_top3_bp'),
                   ('金额前三占比','amount_top3_share'), ('竞价额','amount_yuan'),
                   ('全市场占比','market_amount_share'), ('金额增量','amount_delta_yuan'),
                   ('热板金额比','amount_to_kaipan_ratio'), ('热度','strength'), ('热板涨幅','change_pct'),
                   ('昨日涨停原因数','previous_limit_reason_count'), ('anchor_delta_bp','anchor_delta_bp'), ('龙头状态','anchors_status')]
        head = ''.join('<th>'+cell(label)+'</th>' for label, _ in columns) + '<th>龙头（代码 名称 涨跌幅(bp) 流通市值(亿元)）</th>'
        body = ''.join('<tr>' + ''.join('<td>'+cell(c.get(key))+'</td>' for _, key in columns) +
                       '<td>'+cell('、'.join(' '.join(str(v) if v is not None else '—' for v in (a['symbol'], a['name'], a['change_bp'], round(a['float_cap_yuan']/1e8, 2))) for a in c['anchors']))+'</td></tr>' for c in cards)
        return '<table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table>'
    if view.get('status') != 'available':
        return '<p>数据不可用：'+cell(view.get('reason'))+'</p>'
    def group_table(groups, direction):
        columns = [('板块', 'name'), ('活跃上涨', 'active_up_count'), ('活跃下跌', 'active_down_count'),
                   ('期望数', 'expected_' + direction), ('倍数', 'lift_' + direction), ('p 值', 'p_' + direction)]
        result = ['<table><thead><tr>' + ''.join('<th>' + cell(label) + '</th>' for label, _ in columns) + '</tr></thead><tbody>']
        for group in groups:
            card = group['head']
            names = ''.join('<li>' + cell(c['name']) + '：' + cell(c['p_' + direction]) + '</li>' for c in group['others'])
            detail = '<details><summary>同组板块</summary><ul>' + names + '</ul></details>' if names else ''
            result.append('<tr>' + ''.join('<td>' + cell(card.get(key)) + (detail if key == 'name' else '') + '</td>' for _, key in columns) + '</tr>')
        return ''.join(result) + '</tbody></table>'
    def insignificant_section(cards, p_key):
        return '<details><summary>不显著（折叠）</summary><ul>' + ''.join(
            '<li>' + cell(c['name']) + '：' + cell(c[p_key]) + '</li>' for c in cards) + '</ul></details>'
    parts = ['<section><h2>上涨聚集榜</h2><p>显著：活跃股聚集</p>', group_table(view['up_groups'], 'up'),
             insignificant_section(view['up_insignificant_cards'], 'p_up'),
             '<h2>下跌聚集榜</h2><p>显著：活跃股聚集</p>', group_table(view['down_groups'], 'down'),
             insignificant_section(view['down_insignificant_cards'], 'p_down'),
             '<h2>热板卡片</h2><p>显著：活跃股聚集</p>', table(view['display_cards']), '<details><summary>更多</summary>', table(view['more_cards']), '</details>', insignificant_section(view['insignificant_cards'], 'p_value')]
    parts.append('<h2>活跃个股</h2><table><thead><tr><th>代码</th><th>名称</th><th>涨跌(bp)</th><th>成交额</th><th>封单额</th><th>价格来源</th><th>主题材</th><th>证据</th><th>龙头地位板块</th><th>流通市值(亿元)</th><th>所属热板</th></tr></thead><tbody>')
    for stock in view['active_stocks']:
        values = [stock['symbol'], stock['name'], stock['change_bp'], stock['amount_yuan'], stock['seal_amount_yuan'], stock['price_source'], (stock['primary_theme'] or {}).get('name'), stock['theme_source'], '、'.join(p['name'] or p['plate_code'] for p in stock['anchor_themes']), round(stock['float_cap_yuan']/1e8, 2) if stock['float_cap_yuan'] is not None else None, '、'.join(p['name'] or p['plate_code'] for p in stock['hot_plates'])]
        parts.append('<tr>' + ''.join('<td>'+cell(v)+'</td>' for v in values) + '</tr>')
    parts.append('</tbody></table><details><summary>非题材（保留原排名）</summary><ul>')
    parts.extend('<li>'+cell(c['rank'])+' '+cell(c['name'])+' '+cell(c['non_theme_reason'])+'</li>' for c in view['excluded'])
    parts.append('</ul></details></section>')
    return ''.join(parts)
