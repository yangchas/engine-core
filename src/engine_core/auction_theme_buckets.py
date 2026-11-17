"""Summarize auction data against a caller-supplied point-in-time theme mapping."""
from collections import defaultdict
from statistics import mean, median

from .data_pipeline.auction_seal_facts import classify_limit


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
    from .data_pipeline.plate_stock_index import get_stock_plates
    from .data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
    from .data_pipeline.trade_calendar import TradeCalendarStore
    stage = 'trade_date'
    try:
        day = date.fromisoformat(trade_date).isoformat()
        client = redis_client or make_redis()
        stage = 'auction_view'
        current = read_auction_view(client, day, '0925')
        previous = read_auction_view(client, day, '0924')
        stage = 'mapping'
        mapping = get_stock_plates([r['symbol'] for r in current], day, store=store, redis_client=client, readonly=True)
        if (not mapping or len(mapping) != len(current) or any(
                v.get('status') not in ('PRESENT_REDIS', 'RESTORED_FROM_TD', 'MISSING')
                or (v.get('status') == 'MISSING' and not v.get('version')) for v in mapping.values())):
            raise ValueError('mapping_incomplete')
        plates = {symbol: value['plates'] for symbol, value in mapping.items() if value.get('status') != 'MISSING'}
        stage = 'stock_static'
        with ReferenceSnapshotStore() as ref:
            snapshot = ref.read_as_of('stock_static', f'{day}T23:59:59+08:00')
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
        asof = datetime.fromisoformat(f'{day}T09:05:00+08:00')
        try:
            with ReferenceSnapshotStore() as ref:
                snapshot = ref.read_as_of('plate_registry', asof.isoformat())
                if not snapshot:
                    snapshot = ref.read_latest('plate_registry')
                    if snapshot:
                        visible = datetime.fromisoformat(snapshot['available_at'].replace('Z', '+00:00'))
                        if visible.tzinfo is None or visible > asof:
                            snapshot = None
            if snapshot:
                for row in snapshot['rows']:
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
