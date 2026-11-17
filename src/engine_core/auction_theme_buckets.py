"""Summarize auction data against a caller-supplied point-in-time theme mapping."""
from collections import defaultdict
from decimal import Decimal
from statistics import mean, median

from .limit_price import is_unlimited_listing, limit_prices


def build_theme_buckets(auction_rows, stock_plates, *, top_n=10, universe_count=None):
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
    groups = defaultdict(dict)
    for symbol, plates in stock_plates.items():
        for plate in plates:
            if plate.get('plate_class', plate.get('class')) == 'THEME':
                groups[plate['plate_code']][symbol] = plate
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
            unlimited = is_unlimited_listing(symbol, day, row.get('listing_date'), row.get('trading_dates'))
            side = None
            if unlimited is None:
                limit_unknown += 1
            elif unlimited:
                unlimited_count += 1
            else:
                pre = row.get('pre_close_milli')
                limits = limit_prices(Decimal(pre) / 1000, symbol) if pre else None
                if limits is None:
                    limit_unknown += 1
                elif Decimal(row['price_milli']) / 1000 == Decimal(str(limits[0])):
                    side = 'limit_up'
                elif Decimal(row['price_milli']) / 1000 == Decimal(str(limits[1])):
                    side = 'limit_down'
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
        themes.append({'plate_code': code, 'name': plate.get('name'), 'class': 'THEME',
                       'member_count': len(members), 'auction_count': len(prices),
                       'missing_price_count': len(members) - len(prices), 'amount_yuan': amount,
                       'market_amount_share': amount / market_amount if market_amount else None,
                       'mean_change_bp': mean(values) if values else None,
                       'median_change_bp': median(values) if values else None,
                       'distribution': counts if not limit_unknown else None, 'limit_up_count': counts['limit_up'] if not limit_unknown else None,
                       'limit_down_count': counts['limit_down'] if not limit_unknown else None, 'unknown_limit_count': limit_unknown,
                       'unlimited_count': unlimited_count,
                       'amount_delta_yuan': sum(delta) if len(delta) == len(prices) and prices else None})
    themes.sort(key=lambda r: r['plate_code'])
    denom = universe_count if universe_count is not None else len(current)
    return {'status': 'available', 'themes': themes,
            'market': {'amount_yuan': market_amount, 'auction_count': market_count,
                       'mapped_count': len(set(current) & set(stock_plates)),
                       'universe_count': denom,
                       'mapping_coverage': len(set(current) & set(stock_plates)) / denom if denom else None},
            'top_amount': sorted(themes, key=lambda r: (-r['amount_yuan'], r['plate_code']))[:top_n],
            'top_change': sorted((r for r in themes if r['mean_change_bp'] is not None),
                                 key=lambda r: (-r['mean_change_bp'], r['plate_code']))[:top_n],
            'notes': ['Stocks in multiple themes are counted once in each; theme totals overlap.',
                      'Change buckets are left-closed and right-open; limits take precedence.']}


def read_and_build(trade_date, store=None, redis_client=None):
    # The index API exposes only requested symbols and does not establish the
    # complete as-of universe or supply listing dates and theme names.
    return {'status': 'unavailable', 'reason': 'complete_point_in_time_inputs_unavailable'}
