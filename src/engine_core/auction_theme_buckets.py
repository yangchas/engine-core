"""Summarize auction data against a caller-supplied point-in-time theme mapping."""
from collections import defaultdict
from statistics import mean, median

from .data_pipeline.auction_seal_facts import classify_limit


def normalize_theme_label(label: str) -> str:
    """Normalize plate member label with minimal mechanical rules."""
    if not label:
        return ""
    label = label.strip()
    if label.endswith("概念") and len(label) > 2:
        label = label[:-2]
    if label in ("CPO/MPO", "光芯片", "CPO"):
        return "光模块"
    if label in ("CRO", "CRO/CMO"):
        return "创新药"
    return label


def build_theme_buckets(auction_rows, stock_plates, *, top_n=10, universe_count=None, names=None, listings=None, trading_dates=None):
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 0:
        raise ValueError('invalid_top_n')
    if not stock_plates:
        return {'status': 'unavailable', 'reason': 'mapping_unavailable'}
    quotes = {'0924': {}, '0925': {}}
    for row in auction_rows:
        tag = row.get('auction_tag', '0925')
        if tag in quotes:
            if row['symbol'] in quotes[tag]:
                raise ValueError('duplicate_auction_symbol')
            quotes[tag][row['symbol']] = row
    current, previous = quotes['0925'], quotes['0924']
    market_amount = sum(r['amount_yuan'] for r in current.values())
    market_count = sum(r.get('price_milli') is not None for r in current.values())
    if trading_dates:
        trading_dates = sorted(d for d in trading_dates if d <= str(next(iter(current.values()), {}).get('snapshot_cutoff', ''))[:10])[-5:]
    names = names or {}
    listings = listings or {}
    groups = defaultdict(dict)
    classes = defaultdict(dict)
    for symbol, plates in stock_plates.items():
        for plate in plates:
            kind = plate.get('plate_class', plate.get('class'))
            if kind == 'THEME':
                groups[plate['plate_code']][symbol] = plate
            elif kind in ('NICHE', 'BROAD', 'GENERIC_FACTOR', 'EVENT'):
                classes[kind][symbol] = plate
    themes = []
    for code, members in groups.items():
        prices, values, delta = [], [], []
        counts = {'limit_down': 0, 'below_minus_10': 0, 'minus_10_to_minus_5': 0,
                  'minus_5_to_0': 0, '0_to_5': 0, '5_to_10': 0, 'at_least_10': 0,
                  'limit_up': 0, 'change_unknown': 0}
        limit_unknown = unlimited_count = 0
        for symbol in members:
            row = current.get(symbol)
            if not row or row.get('price_milli') is None:
                continue
            prices.append(row)
            change = row.get('change_bp')
            if change is not None:
                values.append(change)
            if symbol in previous:
                delta.append(row['amount_yuan'] - previous[symbol]['amount_yuan'])
            day = str(row.get('snapshot_cutoff', ''))[:10]
            side = classify_limit(day, symbol, row, listings.get(symbol), trading_dates) if trading_dates else 'unknown_listing'
            if side in ('unknown_listing', 'unknown_price'):
                limit_unknown += 1
                side = None
            elif side == 'unlimited':
                unlimited_count += 1
                side = None
            if side:
                bucket = side
            elif change is None:
                bucket = 'change_unknown'
            elif change < -1000:
                bucket = 'below_minus_10'
            elif change < -500:
                bucket = 'minus_10_to_minus_5'
            elif change < 0:
                bucket = 'minus_5_to_0'
            elif change < 500:
                bucket = '0_to_5'
            elif change < 1000:
                bucket = '5_to_10'
            else:
                bucket = 'at_least_10'
            counts[bucket] += 1
        amount = sum(current[s]['amount_yuan'] for s in members if s in current)
        plate = next(iter(members.values()))
        themes.append({'plate_code': code, 'name': names.get(code), 'class': 'THEME',
                       'member_count': len(members), 'auction_count': len(prices),
                       'missing_price_count': len(members) - len(prices), 'amount_yuan': amount,
                       'market_amount_share': amount / market_amount if market_amount else None,
                       'mean_change_bp': mean(values) if values else None,
                       'median_change_bp': median(values) if values else None,
                       'distribution': counts if not limit_unknown else None, 'limit_up_count': counts['limit_up'],
                       'limit_down_count': counts['limit_down'], 'unknown_limit_count': limit_unknown,
                       'unlimited_count': unlimited_count,
                       'amount_delta_yuan': sum(delta) if len(delta) == len(prices) and prices else None})
    themes.sort(key=lambda r: r['plate_code'])
    denom = universe_count if universe_count is not None else len(current)
    by_class = {}
    for kind, members in classes.items():
        synthetic = {symbol: [{'plate_code': kind, 'plate_class': 'THEME'}] for symbol in members}
        summary = build_theme_buckets(auction_rows, synthetic, top_n=0, universe_count=denom,
                                      listings=listings, trading_dates=trading_dates)['themes'][0]
        summary['class'] = kind
        by_class[kind] = summary
    coverage = sum(row['name'] is not None for row in themes)
    return {'status': 'available', 'themes': themes, 'by_class': by_class,
            'market': {'amount_yuan': market_amount, 'auction_count': market_count,
                       'mapped_count': len(set(current) & set(stock_plates)),
                       'universe_count': denom,
                       'mapping_coverage': len(set(current) & set(stock_plates)) / denom if denom else None},
            'top_amount': sorted(themes, key=lambda r: (-r['amount_yuan'], r['plate_code']))[:top_n],
            'top_change': sorted((r for r in themes if r['mean_change_bp'] is not None),
                                 key=lambda r: (-r['mean_change_bp'], r['plate_code']))[:top_n],
            'notes': [f'names_source=hot_plates; coverage={coverage}/{len(themes)}', 'Stocks in multiple themes are counted once in each; theme totals overlap.',
                      'Change buckets are left-closed and right-open; limits take precedence.']}


def read_and_build(trade_date, store=None, redis_client=None):
    import json
    from datetime import date, datetime
    from .data_pipeline.auction_redis_view import read_auction_view, redis_client as make_redis
    from .data_pipeline.plate_stock_index import get_stock_plates, read_reference_view
    from .data_pipeline.trade_calendar import TradeCalendarStore
    stage = 'trade_date'
    try:
        day = date.fromisoformat(trade_date).isoformat()
        client = redis_client or make_redis()
        stage = 'auction_view'
        current = read_auction_view(client, day, '0925')
        previous = read_auction_view(client, day, '0924')
        stage = 'mapping'
        mapping = get_stock_plates([r['symbol'] for r in current], day, store=store, redis_client=client, readonly=True, hot_only=True)
        if (not mapping or len(mapping) != len(current) or any(
                v.get('status') not in ('PRESENT_REDIS', 'RESTORED_FROM_TD', 'MISSING')
                or (v.get('status') == 'MISSING' and not v.get('version')) for v in mapping.values())):
            raise ValueError('mapping_incomplete')
        plates = {symbol: value['plates'] for symbol, value in mapping.items() if value.get('status') != 'MISSING'}
        stage = 'stock_static'
        snapshot = read_reference_view(client, 'stock_static', day, f'{day}T09:05:00+08:00')
        if not snapshot or not snapshot.get('trade_date') or snapshot['trade_date'] > day:
            raise ValueError('stock_static_unavailable')
        listings = {r['symbol']: r.get('listing_date') for r in snapshot['rows']}
        stage = 'calendar'
        dates = TradeCalendarStore().all_trading_dates()
        if day not in dates or len([d for d in dates if d <= day]) < 5:
            raise ValueError('calendar_coverage_missing')
        # Names are optional and never sourced from the retired catalog.
        names = {}
        registry_available = False
        registry_rows = {}
        asof = datetime.fromisoformat(f'{day}T09:05:00+08:00')
        try:
            snapshot = read_reference_view(client, 'plate_registry', day, asof.isoformat())
            if snapshot:
                for row in snapshot['rows']:
                    code = row.get('plate_code') or row.get('code')
                    if code:
                        registry_rows[str(code)] = row
                    if row.get('name'):
                        for code in (row.get('plate_code'), row.get('code')):
                            if code:
                                names[code] = row['name']
                registry_available = True
        except Exception:
            names = {}
        registry_codes = set(names)
        hot_codes = set()
        try:
            snapshots = []
            for key in client.scan_iter(match=f'core:cache:data:hot_plates:{day}:*', count=100):
                value = json.loads(client.get(key))
                if (value.get('dataset') == 'hot_plates' and value.get('trade_date') == day
                        and value.get('captured_at') and isinstance(value.get('rows'), list)
                        and value.get('row_count') == len(value['rows'])):
                    snapshots.append(value)
            if snapshots:
                latest = max(snapshots, key=lambda item: item['captured_at'])
                for row in latest['rows']:
                    code, name = row.get('plate_code'), row.get('name')
                    if code and name and code not in names:
                        names[code] = name
                        hot_codes.add(code)
        except Exception:
            pass
        stage = 'build'
        result = build_theme_buckets(current + previous, plates, universe_count=len(current),
                                     names=names, listings=listings, trading_dates=dates)
        if result['status'] == 'available':
            for theme in result['themes']:
                record = registry_rows.get(str(theme['plate_code']), {})
                parent = record.get('parent')
                theme['parent'] = parent
                theme['parent_name'] = record.get('parent_name') or names.get(str(parent)) if parent else None
            codes = {row['plate_code'] for row in result['themes']}
            covered = len(codes & registry_codes)
            supplemented = len(codes & hot_codes)
            missing = len(codes - set(names))
            result['notes'][0] = (f'names_source=plate_registry+hot_plates; coverage={covered + supplemented}/{len(codes)}; '
                                  f'registry_coverage={covered}; hot_plates_supplement={supplemented}; missing={missing}')
            if not registry_available:
                result['notes'].append('registry_unavailable_at_asof')
        return result
    except Exception as exc:
        return {'status': 'unavailable', 'reason': f'{stage}:{type(exc).__name__}:{exc}'}


def build_hot_theme_buckets(auction_rows, stock_plates, hot_rows, registry_rows, *, top_n_hot=40,
                            listings=None, trading_dates=None, seal_rows=(), trade_date=None,
                            previous_trade_date=None, pool_labels=None, reason_rows=(),
                            stock_names=None):
    """Aggregate PIT hot plates independently; overlapping members count in each plate."""
    from .auction_theme_cards import non_theme_reason
    from .stock_theme_assignment import build_stock_theme_assignments
    if not isinstance(top_n_hot, int) or isinstance(top_n_hot, bool) or top_n_hot < 0:
        raise ValueError('invalid_top_n_hot')
    if not stock_plates:
        return {'status': 'unavailable', 'reason': 'mapping_unavailable'}
    registry = {str(r.get('plate_code') or r.get('code')): r.get('class', r.get('plate_class')) for r in registry_rows}
    selected, missing, excluded = [], 0, 0
    for row in sorted(hot_rows, key=lambda r: (r['rank'], str(r['plate_code']))):
        kind = registry.get(str(row['plate_code']))
        if not kind:
            missing += 1
        elif kind not in ('THEME', 'NICHE'):
            excluded += 1
        else:
            selected.append(row)
    selected = selected[:top_n_hot]
    current, previous = {}, {}
    for row in auction_rows:
        target = previous if row.get('auction_tag', '0925') == '0924' else current
        if row['symbol'] in target:
            raise ValueError('duplicate_auction_symbol')
        target[row['symbol']] = row
    market_amount = sum(r['amount_yuan'] for r in current.values() if r.get('amount_yuan') is not None)
    listings = listings or {}
    themes = []
    for hot in selected:
        code = str(hot['plate_code'])
        symbols = {s for s, plates in stock_plates.items() if any(str(p['plate_code']) == code for p in plates)}
        priced = [current[s] for s in symbols if s in current and current[s].get('price_milli') is not None]
        changed = [r for r in priced if r.get('change_bp') is not None]
        strong = sum(r['change_bp'] >= 300 for r in changed)
        weak = sum(r['change_bp'] <= -300 for r in changed)
        middle = len(changed) - strong - weak
        amounts = [r['amount_yuan'] for r in priced if r.get('amount_yuan') is not None]
        amount = sum(amounts) if len(amounts) == len(priced) else None
        weighted_rows = [r for r in changed if r.get('amount_yuan') is not None]
        weight = sum(r['amount_yuan'] for r in weighted_rows)
        sides = [classify_limit(trade_date or str(r.get('snapshot_cutoff', ''))[:10], r['symbol'], r,
                                listings.get(r['symbol']), trading_dates) for r in priced] if trading_dates else []
        ordered = sorted(changed, key=lambda r: (-r['change_bp'], r['symbol']))
        deltas = [r['amount_yuan'] - previous[r['symbol']]['amount_yuan'] for r in priced
                  if r.get('amount_yuan') is not None and r['symbol'] in previous and previous[r['symbol']].get('amount_yuan') is not None]
        ka_amount = hot.get('amount_yuan')
        n = len(priced)
        themes.append({'plate_code': code, 'name': hot.get('name'), 'class': registry[code],
                       'non_theme_reason': non_theme_reason(hot.get('name')),
                       'member_count': len(symbols), 'auction_count': n, 'missing_price_count': len(symbols)-n,
                       'missing_change_count': n-len(changed), 'strong_count': strong, 'middle_count': middle, 'weak_count': weak,
                       'strong_share': strong/n if n else None, 'middle_share': middle/n if n else None, 'weak_share': weak/n if n else None,
                       'limit_up_count': sides.count('limit_up'), 'limit_down_count': sides.count('limit_down'),
                       'unknown_limit_count': sum(s in ('unknown_listing','unknown_price') for s in sides) if trading_dates else n,
                       'mean_change_bp': mean(r['change_bp'] for r in changed) if changed else None,
                       'weighted_mean_change_bp': sum(r['change_bp']*r['amount_yuan'] for r in weighted_rows)/weight if weight else None,
                       'mean_ex_top3_bp': mean(r['change_bp'] for r in ordered[3:]) if len(ordered)>3 else None,
                       'amount_top3_share': sum(sorted(amounts, reverse=True)[:3])/amount if amount else None,
                       'amount_yuan': amount, 'market_amount_share': amount/market_amount if amount is not None and market_amount else None,
                       'amount_delta_yuan': sum(deltas) if n and len(deltas)==n else None,
                       'rank': hot.get('rank'), 'strength': hot.get('strength'), 'change_pct': hot.get('change_pct'),
                       'kaipan_amount_yuan': ka_amount, 'amount_to_kaipan_ratio': amount/ka_amount if amount is not None and ka_amount else None})
    members = {s: [{'id': str(p['plate_code']), 'name': h.get('name') or str(p['plate_code'])}
                    for p in plates for h in selected if str(p['plate_code']) == str(h['plate_code'])]
               for s, plates in stock_plates.items()}
    seal_symbols = {r['symbol'] for r in seal_rows if r.get('side') in ('limit_up', 'limit_down')}
    assigned = build_stock_theme_assignments(trade_date=trade_date or '', previous_trade_date=previous_trade_date or '',
                    pool_labels={s:v for s,v in (pool_labels or {}).items() if s in seal_symbols},
                    reason_rows=[r for r in reason_rows if r.get('symbol') in seal_symbols],
                    static_themes={s: [] for s in seal_symbols}, trading_dates=trading_dates)
    seals = [dict(r, name=r.get('name') or (stock_names or {}).get(r['symbol']),
                  assignment=assigned.get(r['symbol']), hot_plates=members.get(r['symbol'], []))
             for r in seal_rows if r.get('side') in ('limit_up', 'limit_down')]
    # Curated Top 10 by auction amount using single source of truth plate_member_label
    stock_to_label = {}
    for sym, p_list in stock_plates.items():
        if isinstance(p_list, list) and p_list:
            for pl in p_list:
                raw_lbl = pl.get('plate_member_label')
                if raw_lbl:
                    first = raw_lbl.replace(',', '、').split('、')[0].strip()
                    norm = normalize_theme_label(first)
                    if norm:
                        stock_to_label[sym] = norm
                        break

    curated_themes = []
    if stock_to_label:
        buckets = defaultdict(list)
        for s, row in current.items():
            if s in stock_to_label and row.get('price_milli') is not None:
                lbl = stock_to_label[s]
                if not non_theme_reason(lbl):
                    buckets[lbl].append(row)

        members_by_label = defaultdict(set)
        for s, lbl in stock_to_label.items():
            if not non_theme_reason(lbl):
                members_by_label[lbl].add(s)

        theme_items = []
        for name, p_rows in buckets.items():
            amt = sum(r['amount_yuan'] for r in p_rows if r.get('amount_yuan') is not None)
            if amt >= 30_000_000 and len(p_rows) >= 5:
                theme_items.append((name, amt, p_rows))

        theme_items.sort(key=lambda x: -x[1])

        for rank_idx, (name, amt, p_rows) in enumerate(theme_items[:10], 1):
            n = len(p_rows)
            changed = [r for r in p_rows if r.get('change_bp') is not None]
            strong = sum(r['change_bp'] >= 300 for r in changed)
            weak = sum(r['change_bp'] <= -300 for r in changed)
            middle = len(changed) - strong - weak

            up_count = sum(r['change_bp'] > 0 for r in changed)
            down_count = sum(r['change_bp'] < 0 for r in changed)
            flat_count = sum(r['change_bp'] == 0 for r in changed)
            up_ratio = up_count / len(changed) if changed else 0.0

            weighted_rows = [r for r in changed if r.get('amount_yuan') is not None]
            weight = sum(r['amount_yuan'] for r in weighted_rows)
            w_bp = sum(r['change_bp'] * r['amount_yuan'] for r in weighted_rows) / weight if weight else None
            m_bp = mean(r['change_bp'] for r in changed) if changed else None

            sides = [classify_limit(trade_date or str(r.get('snapshot_cutoff', ''))[:10], r['symbol'], r,
                                    listings.get(r['symbol']), trading_dates) for r in p_rows] if trading_dates else []
            limit_up_cnt = sides.count('limit_up')
            limit_down_cnt = sides.count('limit_down')

            sorted_by_amt = sorted(p_rows, key=lambda r: (r.get('amount_yuan') or 0), reverse=True)
            top1 = sorted_by_amt[0] if sorted_by_amt else None
            top1_amt = top1.get('amount_yuan') or 0 if top1 else 0
            top1_share = top1_amt / amt if amt else 0.0
            top1_sym = top1['symbol'] if top1 else None
            top1_name = (stock_names or {}).get(top1_sym) or top1_sym
            top1_chg = (top1.get('change_bp') or 0) / 100 if top1 and top1.get('change_bp') is not None else None

            top3_amts = [r.get('amount_yuan') or 0 for r in sorted_by_amt[:3]]
            top3_share = sum(top3_amts) / amt if amt else None

            curated_themes.append({
                'plate_code': name,
                'name': name,
                'class': 'THEME',
                'rank': rank_idx,
                'member_count': len(members_by_label.get(name, p_rows)),
                'auction_count': n,
                'amount_yuan': amt,
                'market_amount_share': amt / market_amount if market_amount else None,
                'mean_change_bp': m_bp,
                'weighted_mean_change_bp': w_bp,
                'strong_count': strong,
                'middle_count': middle,
                'weak_count': weak,
                'strong_share': strong / n if n else None,
                'middle_share': middle / n if n else None,
                'weak_share': weak / n if n else None,
                'up_count': up_count,
                'down_count': down_count,
                'flat_count': flat_count,
                'up_ratio': up_ratio,
                'limit_up_count': limit_up_cnt,
                'limit_down_count': limit_down_cnt,
                'top1_symbol': top1_sym,
                'top1_name': top1_name,
                'top1_amount_yuan': top1_amt,
                'top1_share': top1_share,
                'top1_change_pct': top1_chg,
                'amount_top3_share': top3_share,
            })

    if not curated_themes:
        curated_themes = [t for t in themes if not t.get('non_theme_reason')
                          and (t.get('amount_yuan') or 0) >= 30_000_000
                          and (t.get('auction_count') or 0) >= 5]
    return {'status':'available', 'themes':themes, 'curated_themes':curated_themes, 'market':{'amount_yuan':market_amount}, 'seals':seals,
            'notes':[f'category_missing={missing}; category_excluded={excluded}', f'curated_count={len(curated_themes)}/{len(themes)}', 'Stocks in multiple hot plates are counted once in each; totals overlap.']}


def read_and_build_hot(trade_date, store=None, redis_client=None, top_n_hot=40, *,
                       tolerance_seconds=0, max_wait_seconds=0, poll_interval=0.5):
    """Read versions visible by 09:26 (with optional tolerance/wait); never consult retired catalog."""
    import gzip
    import json
    import time
    from datetime import date, datetime, timedelta
    from pathlib import Path
    from .data_pipeline.auction_redis_view import read_auction_view, redis_client as make_redis
    from .data_pipeline.plate_stock_index import get_stock_plates, read_reference_view
    from .data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
    from .data_pipeline.trade_calendar import TradeCalendarStore
    notes, stage = [], 'trade_date'
    try:
        day = date.fromisoformat(trade_date).isoformat()
        base_cutoff = datetime.fromisoformat(f'{day}T09:26:00+08:00')
        cutoff = base_cutoff + timedelta(seconds=tolerance_seconds) if tolerance_seconds else base_cutoff
        client = redis_client or make_redis()
        stage = 'hot_plates'
        deadline = time.monotonic() + max_wait_seconds if max_wait_seconds > 0 else 0
        while True:
            versions = []
            for key in client.scan_iter(match=f'core:cache:data:hot_plates:{day}:*', count=100):
                value = json.loads(client.get(key))
                stamp = datetime.fromisoformat(value['available_at'].replace('Z', '+00:00'))
                if (stamp.tzinfo is not None and stamp <= cutoff and value.get('trade_date') == day
                        and value.get('dataset') == 'hot_plates' and isinstance(value.get('rows'), list)
                        and value.get('row_count') == len(value['rows'])):
                    versions.append((stamp, str(value.get('version', '')), value))
            if versions or time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)
        if not versions:
            raise ValueError('no_visible_hot_version')
        hot = max(versions, key=lambda x: (x[0], x[1]))[2]
        notes.append(f"hot_version={hot.get('version')}; event_id={hot.get('event_id')}; available_at={hot['available_at']}; cutoff={cutoff.isoformat()}")
        stage = 'auction_view'
        current = read_auction_view(client, day, '0925')
        previous = read_auction_view(client, day, '0924')
        stage = 'mapping'
        mapping = get_stock_plates([r['symbol'] for r in current], day, store=store, redis_client=client, readonly=True, hot_only=True)
        if not mapping or len(mapping) != len(current) or any(v.get('status') not in ('PRESENT_REDIS','RESTORED_FROM_TD','MISSING') or (v.get('status') == 'MISSING' and not v.get('version')) for v in mapping.values()):
            raise ValueError('mapping_incomplete')
        plates = {s:v['plates'] for s,v in mapping.items() if v.get('status') != 'MISSING'}
        stage = 'reference_snapshots'
        registry = read_reference_view(client, 'plate_registry', day, cutoff.isoformat())
        static = read_reference_view(client, 'stock_static', day, cutoff.isoformat())
        if not registry or not static:
            with ReferenceSnapshotStore() as ref:
                if not registry:
                    registry = ref.read_as_of('plate_registry', cutoff.isoformat())
                if not static:
                    static = ref.read_as_of('stock_static', cutoff.isoformat())
        if not registry:
            raise ValueError('plate_registry_unavailable_at_cutoff')
        if not static or not static.get('trade_date') or static['trade_date'] > day:
            raise ValueError('stock_static_unavailable_at_cutoff')
        stage = 'calendar'
        dates = sorted(d for d in TradeCalendarStore().all_trading_dates() if d <= day)
        if day not in dates or len(dates) < 5:
            raise ValueError('calendar_coverage_missing')
        prev_day = dates[-2]
        stage = 'seal_facts'
        seal = json.loads(client.get(f'core:view:auction_seal_facts:{day}'))
        seal_at = datetime.fromisoformat(seal['available_at'].replace('Z', '+00:00'))
        if seal_at.tzinfo is None or seal_at > cutoff or seal.get('trade_date') != day or seal.get('row_count') != len(seal['rows']):
            raise ValueError('seal_facts_not_visible')
        # Resolve only registry codes or unambiguous registry names; attribution may be outside the hot vocabulary.
        by_code = {str(r.get('plate_code') or r.get('code')): {'id':str(r.get('plate_code') or r.get('code')), 'name':r.get('name') or str(r.get('plate_code') or r.get('code'))} for r in registry['rows'] if r.get('plate_code') or r.get('code')}
        by_name = defaultdict(list)
        for item in by_code.values():
            by_name[item['name']].append(item)
        pool_labels, reasons = {}, []
        stage = 'assignment_evidence'
        raw = client.get(f'core:view:limit_up_pool:{prev_day}')
        if raw:
            pool = json.loads(raw)
            visible = datetime.fromisoformat(pool['available_at'].replace('Z', '+00:00'))
            if visible.tzinfo is not None and visible <= cutoff and pool.get('trade_date') == prev_day and pool.get('row_count') == len(pool['rows']):
                for r in pool['rows']:
                    candidates = []
                    if str(r.get('plate_code') or '') in by_code:
                        candidates.append(by_code[str(r['plate_code'])])
                    for name in [r.get('reason_category')] + str(r.get('reason_plates_text') or '').replace('，','、').split('、'):
                        if len(by_name.get(name, [])) == 1:
                            candidates.append(by_name[name][0])
                    pool_labels[r['symbol']] = candidates
        for prior in dates[:-1][-20:]:
            pointer = client.get(f'core:cache:data:limit_reasons:{prior}')
            if not pointer:
                continue
            manifest = json.loads(Path(json.loads(pointer)['manifest']).read_text())
            visible = datetime.fromisoformat(manifest['available_at'].replace('Z', '+00:00'))
            if visible.tzinfo is None or visible > cutoff or manifest.get('trade_date') != prior:
                continue
            with gzip.open(manifest['rows_uri'], 'rt') as f:
                for line in f:
                    r = json.loads(line)
                    candidates = [by_code[str(c)] for c in r.get('plate_codes', []) if str(c) in by_code]
                    name = r.get('main_reason') or r.get('reason')
                    if len(by_name.get(name, [])) == 1:
                        candidates += by_name[name]
                    reasons.append({'symbol':r['symbol'], 'Date':r.get('event_date') or r.get('date'), 'ZSCode':candidates})
        stage = 'build'
        stock_names = {r['symbol']: r.get('name') for r in static.get('rows', []) if r.get('symbol')}
        result = build_hot_theme_buckets(current+previous, plates, hot['rows'], registry['rows'], top_n_hot=top_n_hot,
            listings={r['symbol']:r.get('listing_date') for r in static['rows']}, trading_dates=dates,
            seal_rows=seal['rows'], trade_date=day, previous_trade_date=prev_day, pool_labels=pool_labels, reason_rows=reasons,
            stock_names=stock_names)
        result.setdefault('notes', []).extend(notes)
        return result
    except Exception as exc:
        return {'status':'unavailable', 'reason':f'{stage}:{type(exc).__name__}:{exc}', 'notes':notes}
