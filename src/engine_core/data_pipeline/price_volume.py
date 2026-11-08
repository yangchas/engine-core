"""Incremental first-batch price indicators over adjusted daily bars.

The caller supplies trading-calendar continuity: an unknown missing trading day
must reset the checkpoint before the next bar. UNPROCESSED skips the bar;
NO_EVENTS uses 1.0; BEFORE_FIRST_EVENT uses the first event. These calculated
non-OK factors mark the row PARTIAL. Snapshots contain one version per symbol.
"""
from __future__ import annotations

import math

from .adjust_factor import factor_for


_FIELDS = ('ma5', 'ma10', 'ma20', 'ma60', 'ema12', 'ema26', 'dif', 'dea',
           'macd', 'k', 'd', 'j', 'boll_upper', 'boll_middle', 'boll_lower',
           'boll_bandwidth')


def calculate_bar(bar, events, state=None, *, gap_before=False, factor_quality=None):
    """Return (indicators, JSON-serializable checkpoint) for one observed bar."""
    state = dict(state or {})
    if bar is None or bar.get('suspended') or events is None or factor_quality == 'UNPROCESSED':
        return None, state
    if gap_before:
        state = {}
    result = {name: None for name in _FIELDS}
    result['adjusted_close'] = None
    factor, quality = factor_for(events, str(bar['ts'])[:10])
    if quality == 'NO_EVENTS':
        factor = 1.0
    elif quality == 'BEFORE_FIRST_EVENT':
        first = min(events, key=lambda event: event['dividOperateDate'])
        factor, _ = factor_for([first], first['dividOperateDate'])
    result['factor_quality'] = quality
    result['partial_reasons'] = [] if quality in ('FORMAL', 'OK') else ['FACTOR_' + quality]
    if factor is None:
        return None, state

    def adjusted(name):
        value = bar.get(name)
        if value is None:
            return None
        value = float(value) * factor
        return value if math.isfinite(value) else None

    close, high, low = (adjusted(name) for name in ('close', 'high', 'low'))
    result['adjusted_close'] = close
    window = list(state.get('window', []))
    window.append([close, high, low])
    window = window[-60:]
    state['window'] = window
    if close is None:
        for key in ('ema12', 'ema26', 'dea'):
            state.pop(key, None)
    else:
        for period in (12, 26):
            key = f'ema{period}'
            previous = state.get(key)
            state[key] = close if previous is None else (2 / (period + 1) * close + (1 - 2 / (period + 1)) * previous)
            result[key] = state[key]
        result['dif'] = state['ema12'] - state['ema26']
        previous = state.get('dea')
        state['dea'] = result['dif'] if previous is None else 0.2 * result['dif'] + 0.8 * previous
        result['dea'] = state['dea']
        result['macd'] = 2 * (result['dif'] - state['dea'])
    for period in (5, 10, 20, 60):
        values = [row[0] for row in window[-period:]]
        if len(values) == period and all(v is not None for v in values):
            result[f'ma{period}'] = sum(values) / period
    if result['ma20'] is not None:
        mean = result['ma20']
        deviation = math.sqrt(sum((row[0] - mean) ** 2 for row in window[-20:]) / 20)
        result['boll_middle'] = mean
        result['boll_upper'] = mean + 2 * deviation
        result['boll_lower'] = mean - 2 * deviation
        if mean != 0:
            result['boll_bandwidth'] = (result['boll_upper'] - result['boll_lower']) / mean
    recent = window[-9:]
    if len(recent) == 9 and all(all(v is not None for v in row) for row in recent):
        highest = max(row[1] for row in recent)
        lowest = min(row[2] for row in recent)
        if highest != lowest:
            rsv = 100 * (close - lowest) / (highest - lowest)
            state['k'] = (2 * state.get('k', 50) + rsv) / 3
            state['d'] = (2 * state.get('d', 50) + state['k']) / 3
            result.update(k=state['k'], d=state['d'], j=3 * state['k'] - 2 * state['d'])
    if result['k'] is None:
        state.pop('k', None)
        state.pop('d', None)
    result['quality'] = 'FORMAL' if not result['partial_reasons'] and all(result[name] is not None for name in _FIELDS) else 'PARTIAL'
    return result, state


# A single stable primary timestamp makes snapshot replacement an upsert.  The
# checkpoint's actual last trade date is part of the JSON, never the TD ts.
STATE_TIMESTAMP_US = 1794067200000000  # 2026-11-08, valid processing epoch
STATE_JSON_CAPACITY = 60000
_RESULT_COLUMNS = (
    'ma5', 'ma10', 'ma20', 'ma60', 'ema12', 'ema26', 'dif', 'dea', 'macd',
    'k', 'd', 'j', 'rsi6', 'rsi12', 'rsi24', 'boll_upper', 'boll_middle',
    'boll_lower', 'boll_bandwidth', 'atr14', 'pct_chg_volatility20',
    'volume_ma5', 'volume_ma10', 'volume_ma20', 'volume_ratio', 'bias_20',
    'rsi_6', 'change_pct_5d', 'avg_turnover_5d', 'limit_up_days_5', 'vol_ratio',
    'state_ema12', 'state_ema26', 'state_dea', 'state_k', 'state_d',
    'state_rsi6_gain', 'state_rsi6_loss', 'state_rsi12_gain', 'state_rsi12_loss',
    'state_rsi24_gain', 'state_rsi24_loss', 'state_atr14', 'state_prev_close', 'state_count',
)


class PriceVolumeStore:
    """TD persistence; the supplied TaosStore owns connection and database."""

    def __init__(self, store):
        from .td_sink import validate_database_name
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        definitions = ', '.join(name + (' SMALLINT' if name == 'limit_up_days_5' else
                                       ' INT' if name == 'state_count' else ' DOUBLE')
                                for name in _RESULT_COLUMNS)
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.price_volume_indicators_v1 '
                        '(ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), '
                        'input_refs NCHAR(12000), available_at VARCHAR(40), quality VARCHAR(16), '
                        f'warm BOOL, {definitions}) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.price_volume_state_v1 '
                        f'(ts TIMESTAMP, state_json VARCHAR({STATE_JSON_CAPACITY})) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.price_volume_days '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), '
                        'business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), '
                        'available_at VARCHAR(40), quality VARCHAR(4000), missing_whitelist VARCHAR(4000))')

    def read_states(self, symbols):
        import json
        from .adjust_factor import AdjustFactorStore
        from .td_sink import _literal
        result = {}
        symbols = sorted({AdjustFactorStore._symbol(s) for s in symbols})
        for start in range(0, len(symbols), 200):
            selected = ', '.join(_literal(s) for s in symbols[start:start + 200])
            sql = (f'SELECT symbol, LAST_ROW(state_json) FROM {self.database}.price_volume_state_v1 '
                   f'WHERE symbol IN ({selected})')
            rows, _ = self.store._day_scan(sql, suffix=' GROUP BY tbname, symbol', strict=True)
            for symbol, payload in rows:
                result[str(symbol)] = json.loads(payload)
        return result

    def write_states(self, states):
        import json
        from .adjust_factor import AdjustFactorStore
        from .td_sink import INSERT_CHUNK_ROWS, _literal
        items = sorted(states.items())
        for start in range(0, len(items), INSERT_CHUNK_ROWS):
            parts = []
            for symbol, state in items[start:start + INSERT_CHUNK_ROWS]:
                symbol = AdjustFactorStore._symbol(symbol)
                payload = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                if len(payload.encode('utf-8')) > STATE_JSON_CAPACITY:
                    raise ValueError('price_volume_state_json_exceeds_capacity')
                parts.append(f'{self.database}.price_volume_state_{symbol} USING '
                             f'{self.database}.price_volume_state_v1 TAGS({_literal(symbol)}) '
                             f'VALUES ({STATE_TIMESTAMP_US}, {_literal(payload)})')
            if parts:
                self.store._run('INSERT INTO ' + ' '.join(parts))

    def write_results(self, day, rows):
        from .adjust_factor import AdjustFactorStore
        from .td_sink import INSERT_CHUNK_ROWS, _literal, trade_day_ts_us
        items = sorted(rows.items())
        for start in range(0, len(items), INSERT_CHUNK_ROWS):
            parts = []
            for symbol, row in items[start:start + INSERT_CHUNK_ROWS]:
                symbol = AdjustFactorStore._symbol(symbol)
                values = [row.get(key) for key in ('version', 'algo_version', 'input_refs',
                                                    'available_at', 'quality', 'warm') + _RESULT_COLUMNS]
                parts.append(f'{self.database}.price_volume_indicators_{symbol} USING '
                             f'{self.database}.price_volume_indicators_v1 TAGS({_literal(symbol)}) '
                             f'VALUES ({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')
            if parts:
                self.store._run('INSERT INTO ' + ' '.join(parts))

    def write_day_marker(self, day, version, business_status, row_count, sha256, available_at, quality,
                         missing_whitelist='[]'):
        from .td_sink import _literal, trade_day_ts_us
        if len(quality) > 4000 or len(missing_whitelist) > 4000:
            raise ValueError('marker_too_large')
        values = [day, version, business_status, row_count, sha256, available_at, quality, missing_whitelist]
        self.store._run(f'INSERT INTO {self.database}.price_volume_days VALUES '
                        f'({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')


def run(request, root, *, store=None, clock=None, dry_run=False):
    """Calculate one day from batch reads and publish the day marker last."""
    import hashlib
    import json
    import time
    from datetime import date, datetime, timezone
    from .auction_amount_facts import _reply, _registered_failure, core_td_database
    from .td_sink import TaosStore, TdResourceExhausted, td_memory_exhausted
    clock = clock or time.monotonic
    started = clock()
    detail = {'computed': 0, 'skipped_no_bar': 0, 'skipped_factor_unprocessed': 0,
              'partial_total': 0, 'partial_reasons': {}, 'recompute_registered': {'count': 0, 'samples': []},
              'listing_source': 'fallback', 'elapsed_seconds': 0, 'aborted': False}
    try:
        day = request['trade_date']
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid trade date')
    except (KeyError, TypeError, ValueError) as exc:
        detail['message'] = str(exc)
        return _reply(64, 'CONFIG_ERROR', detail)
    if dry_run:
        return _reply(0, detail=detail)
    owned = store is None
    facts = store if store is not None else PriceVolumeStore(TaosStore(core_td_database()))
    try:
        facts.ensure_schema()
        if owned:
            from .adjust_factor import AdjustFactorStore
            bars = facts.store.read_day_rows(day)
            factors = AdjustFactorStore(facts.store).read_events_many(bars)
        else:
            bars = facts.read_day_rows(day)
            factors = facts.read_events_many(bars)
        states = facts.read_states(bars)
        gaps = set()
        dated = {s: state['last_trade_date'] for s, state in states.items()
                 if state.get('last_trade_date') and state['last_trade_date'] < day}
        if dated:
            from .stock_listing import listed_on, read_active
            listing = None
            snapshot_store = None
            try:
                if owned:
                    from .reference_snapshot_td import ReferenceSnapshotStore
                    snapshot_store = ReferenceSnapshotStore()
                    listing = read_active(snapshot_store)
                elif hasattr(facts, 'read_listing'):
                    listing = facts.read_listing()
                if listing and listing.get('rows'):
                    listed_on(listing['rows'], day)
                    detail['listing_source'] = 'snapshot'
                else:
                    listing = None
            except Exception:
                listing = None
            finally:
                if snapshot_store is not None:
                    snapshot_store.close()
            if owned:
                from .trade_calendar import TradeCalendarStore
                previous_day = TradeCalendarStore(root).previous_trade_date
                read_day = facts.store.read_day_rows
            else:
                previous_day = facts.previous_day
                read_day = facts.read_day_rows
            earliest = min(dated.values())
            cursor = str(previous_day(day))[:10]
            while cursor > earliest:
                candidates = {s for s, last in dated.items() if last < cursor and s not in gaps}
                if candidates:
                    historical = read_day(cursor)
                    listed = ({s.rsplit('.', 1)[-1].zfill(6) for s in listed_on(listing['rows'], cursor)}
                              if listing is not None else candidates)
                    for symbol in candidates:
                        if symbol in listed and symbol not in historical:
                            gaps.add(symbol)
                cursor = str(previous_day(cursor))[:10]
        now = datetime.now(timezone.utc)
        available_at = now.isoformat()
        version = now.strftime('%Y%m%d%H%M%S%f')[:20]
        rows, new_states = {}, {}
        for symbol, source in sorted(bars.items()):
            bar = dict(source, ts=day)
            events, quality = factors.get(symbol, (None, 'UNPROCESSED'))
            if quality == 'UNPROCESSED':
                detail['skipped_factor_unprocessed'] += 1
                continue
            result, state = calculate_bar(bar, events, states.get(symbol), gap_before=symbol in gaps, factor_quality=quality)
            if result is None:
                detail['skipped_no_bar'] += 1
                continue
            previous_close = states.get(symbol, {}).get('previous_close')
            pre_close = bar.get('pre_close')
            if (previous_close is not None and pre_close is not None and float(previous_close) != 0
                    and abs(float(pre_close) - float(previous_close)) / abs(float(previous_close)) > 1e-6
                    and not any(str(event.get('dividOperateDate'))[:10] == day for event in events)):
                result['partial_reasons'].append('FACTOR_MAY_BE_STALE')
                result['quality'] = 'PARTIAL'
                registry = detail['recompute_registered']
                registry['count'] += 1
                if len(registry['samples']) < 20:
                    registry['samples'].append(symbol)
            state['last_trade_date'] = day
            state['previous_close'] = bar.get('close')
            result.update(version=version, algo_version='price_volume_v1',
                          input_refs=json.dumps({'daily_kline': bar.get('version')}, separators=(',', ':')),
                          available_at=available_at, warm=result['quality'] == 'FORMAL')
            for key in ('ema12', 'ema26', 'dea', 'k', 'd'):
                result['state_' + key] = state.get(key)
            rows[symbol], new_states[symbol] = result, state
            if result['quality'] == 'PARTIAL':
                detail['partial_total'] += 1
                for reason in result['partial_reasons']:
                    detail['partial_reasons'][reason] = detail['partial_reasons'].get(reason, 0) + 1
        facts.write_results(day, rows)
        facts.write_states(new_states)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        detail['computed'] = len(rows)
        detail['elapsed_seconds'] = round(clock() - started, 3)
        facts.write_day_marker(day, version, 'PARTIAL' if detail['partial_total'] else 'FORMAL', len(rows),
                               digest, available_at, json.dumps(detail, separators=(',', ':')))
        return _reply(0, detail=detail)
    except Exception as exc:
        detail['aborted'] = True
        detail['elapsed_seconds'] = round(clock() - started, 3)
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _registered_failure('TD_RESOURCE_EXHAUSTED', detail)
        raise
    finally:
        if owned:
            facts.store.close()
