"""Auction limit-price candidates and observable final order-book seals."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from ..limit_price import limit_prices


def classify_limit(trade_date, symbol, quote, listing, trading_dates):
    """Return limit side, unlimited, unknown_listing, unknown_price, or None."""
    date.fromisoformat(trade_date)
    dates = sorted(d for d in trading_dates if d <= trade_date)
    if trade_date not in dates or len(dates) < 5:
        raise ValueError('calendar_coverage_missing')
    try:
        if not listing or date.fromisoformat(listing).isoformat() > trade_date:
            return 'unknown_listing'
    except (ValueError, TypeError):
        return 'unknown_listing'
    if listing >= dates[-5]:
        return 'unlimited'
    price, pre = quote.get('price_milli'), quote.get('pre_close_milli')
    if price is None or pre is None:
        return 'unknown_price'
    limits = limit_prices(Decimal(pre) / 1000, symbol)
    if not limits:
        return 'unknown_price'
    if Decimal(price) / 1000 == Decimal(str(limits[0])):
        return 'limit_up'
    if Decimal(price) / 1000 == Decimal(str(limits[1])):
        return 'limit_down'
    return None


def build_facts(trade_date, auction, listings, trading_dates, fetch_ticks):
    date.fromisoformat(trade_date)
    trading_dates = sorted(d for d in trading_dates if d <= trade_date)[-5:]
    if trade_date not in trading_dates or len(trading_dates) < 5:
        raise ValueError('calendar_coverage_missing')
    rows, unknown, unlimited, unknown_prices = [], [], [], []
    for symbol, quote in sorted(auction.items()):
        side = classify_limit(trade_date, symbol, quote, listings.get(symbol), trading_dates)
        if side == 'unknown_listing':
            unknown.append(symbol)
            continue
        if side == 'unlimited':
            unlimited.append(symbol)
            continue
        if side == 'unknown_price':
            unknown_prices.append(symbol)
            continue
        price, pre = quote.get('price_milli'), quote.get('pre_close_milli')
        if not side:
            continue
        ticks = sorted(fetch_ticks(symbol), key=lambda r: str(r['ts']))
        final = [t for t in ticks if f'{trade_date} 09:25:00' <= str(t['ts'])[:19] < f'{trade_date} 09:26:00']
        pre_ticks = [t for t in ticks if str(t['ts'])[:19] == f'{trade_date} 09:24:57']
        tick = final[0] if final else pre_ticks[-1] if pre_ticks else None
        basis = 'final_0925' if final else 'pre_0924_57' if pre_ticks else None
        volume = amount = stamp = None
        if tick:
            stamp = datetime.fromisoformat(str(tick['ts'])).replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
            if tick.get('px_milli') == price:
                key = ('bv1' if final else 'bv2') if side == 'limit_up' else ('av1' if final else 'av2')
                lots = tick.get(key)
                if isinstance(lots, int) and not isinstance(lots, bool) and lots >= 0:
                    volume = lots * 100
                    book_price = tick.get('bp1_milli' if side == 'limit_up' else 'ap1_milli')
                    if isinstance(book_price, int) and book_price > 0:
                        amount = float(Decimal(volume) * Decimal(book_price) / 1000)
                    else:
                        volume = None
            else:
                basis = None
        rows.append(dict(symbol=symbol, side=side, price=float(Decimal(price)/1000),
                         pre_close=float(Decimal(pre)/1000), price_source=quote.get('src') if quote.get('src') in ('auction_match', 'indicative_bid_ask_mid') else 'missing', seal_volume=volume,
                         seal_amount_yuan=amount, source_tick_ts=stamp, basis=basis))
    return {'rows': rows, 'unknown_listing': unknown, 'unknown_prices': unknown_prices, 'unlimited': unlimited}


def run(trade_date, *, redis_client=None, store=None):
    """Read the specified day's inputs and publish; the caller controls storage."""
    from .auction_redis_view import read_auction_view, redis_client as make_redis
    from .reference_snapshot_td import ReferenceSnapshotStore
    from .td_sink import connect_td
    from .trade_calendar import TradeCalendarStore
    from ..storage.two_layer import TwoLayerStore
    day = date.fromisoformat(trade_date).isoformat()
    client = redis_client or make_redis()
    quotes = read_auction_view(client, day, '0925')
    with ReferenceSnapshotStore() as ref:
        snapshot = ref.read_as_of('stock_static', f'{day}T23:59:59+08:00')
    if not snapshot or not snapshot['trade_date'] or snapshot['trade_date'] > day:
        raise ValueError('stock_static_date_unavailable')
    listings = {row['symbol']: row.get('listing_date') for row in snapshot['rows']}
    dates = TradeCalendarStore().all_trading_dates()
    conn = connect_td('market_data1')
    columns = ('ts', 'px_milli', 'bp1_milli', 'ap1_milli', 'bv1', 'bv2', 'av1', 'av2')
    def fetch(symbol):
        if not re.fullmatch(r'[0-9]{6}', symbol):
            raise ValueError('invalid_symbol')
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT ts,px_milli,bp1_milli,ap1_milli,bv1,bv2,av1,av2 FROM market_data1.stock_tick_v2 "
                           f"WHERE symbol='{symbol}' AND ts>='{day} 09:24:30' AND ts<='{day} 09:26:00' ORDER BY ts")
            ticks = [dict(zip(columns, row)) for row in cursor.fetchall()]
            if not any(f'{day} 09:25:00' <= str(t['ts'])[:19] < f'{day} 09:26:00'
                       or str(t['ts'])[:19] == f'{day} 09:24:57' for t in ticks):
                raise ConnectionError(f'auction_tick_unavailable:{symbol}')
            return ticks
        finally:
            cursor.close()
    try:
        result = build_facts(day, {row['symbol']: row for row in quotes}, listings, dates, fetch)
    finally:
        conn.close()
    metadata = {'version': 'auction_seal_facts_v1', 'available_at': f'{day}T09:26:00+08:00',
                'unknown_listing': result['unknown_listing'], 'unknown_prices': result['unknown_prices'],
                'unlimited': result['unlimited']}
    return (store or TwoLayerStore(client)).publish('auction_seal_facts', day, result['rows'], metadata)
