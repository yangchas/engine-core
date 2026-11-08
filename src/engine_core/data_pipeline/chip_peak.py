"""Vectorized chip distribution calculation over independent price windows."""
import numpy as np

FIELDS = ('peak_price', 'peak_weight_pct', 'chip_percent', 'avg_cost',
          'profit_ratio', 'loss_ratio', 'cost_low_70', 'cost_high_70',
          'concentration_70', 'dense_bins')


def calculate_windows(prices, turnovers):
    """Calculate one distribution per ascending, already adjusted price window."""
    if len(prices) != len(turnovers):
        raise ValueError('window count mismatch')
    results = []
    valid = []
    for index, (p, t) in enumerate(zip(prices, turnovers, strict=True)):
        if len(p) != len(t) or len(p) > 120:
            raise ValueError('invalid window length')
        result = dict.fromkeys(FIELDS)
        result.update(bars_used=len(p), quality='PARTIAL', warm=False)
        results.append(result)
        try:
            pp = np.array(p, dtype=float)
            tt = np.array(t, dtype=float)
            ok = len(p) >= 5 and np.isfinite(pp).all() and np.isfinite(tt).all() and (tt >= 0).all()
        except (TypeError, ValueError):
            ok = False
        if not ok:
            continue
        lo, hi = float(pp.min()), float(pp.max())
        if lo == hi:
            result.update(peak_price=lo, concentration_70=0, dense_bins=1)
            continue
        valid.append((index, pp, tt, lo, hi))
    if not valid:
        return results
    n = len(valid)
    lo = np.array([v[3] for v in valid]); hi = np.array([v[4] for v in valid])
    p = np.repeat(lo[:, None], 120, axis=1)
    t = np.zeros((n, 120))
    for i, (_, pp, tt, _, _) in enumerate(valid):
        p[i, -len(pp):] = pp
        t[i, -len(tt):] = tt
    edges = np.array([np.linspace(a, b, 51) for a, b in zip(lo, hi, strict=True)])
    centers = (edges[:, :-1] + edges[:, 1:]) / 2
    weights = np.zeros((n, 50))
    rows = np.arange(n)
    for step in range(120):
        turn = np.minimum(t[:, step] / 100, 1)
        weights *= (1 - turn[:, None])
        bins = np.minimum(((p[:, step] - lo) / (hi - lo) * 50).astype(int), 49)
        weights[rows, bins] += turn
    cumulative = np.cumsum(weights, axis=1)
    total = cumulative[:, -1]
    peak = np.argmax(weights, axis=1)
    for i, (index, pp, _, _, _) in enumerate(valid):
        out = results[index]
        peak_price = round(float(centers[i, peak[i]]), 2)
        if total[i] <= 0:
            out.update(peak_price=peak_price, peak_weight_pct=0, chip_percent=0,
                       avg_cost=peak_price, profit_ratio=0, loss_ratio=0,
                       cost_low_70=peak_price, cost_high_70=peak_price,
                       concentration_70=1, dense_bins=0)
            continue
        percent = round(float(weights[i, peak[i]] / total[i] * 100), 2)
        cost = np.cumsum(centers[i] * weights[i])[-1]
        profit = np.cumsum(np.where(centers[i] <= pp[-1], weights[i], 0))[-1]
        ratio = round(float(profit / total[i]), 4)
        low = min(int(np.searchsorted(cumulative[i], total[i] * .15)), 49)
        high = min(int(np.searchsorted(cumulative[i], total[i] * .85)), 49)
        ranked = np.cumsum(np.sort(weights[i])[::-1])
        rank = min(int(np.searchsorted(ranked, total[i] * .7)), 49)
        out.update(peak_price=peak_price, peak_weight_pct=percent, chip_percent=percent,
                   avg_cost=round(float(cost / total[i]), 2), profit_ratio=ratio,
                   loss_ratio=round(max(0, 1 - ratio), 4),
                   cost_low_70=round(float(centers[i, low]), 2),
                   cost_high_70=round(float(centers[i, high]), 2),
                   concentration_70=round((rank + 1) / 50, 4),
                   dense_bins=int(np.count_nonzero(weights[i] > total[i] * .05)))
        if len(pp) == 120:
            out.update(quality='FORMAL', warm=True)
    return results


def prepare_window(bar, events, state=None, *, factor_quality=None):
    """Append a raw bar and adjust the entire checkpoint with the current snapshot."""
    from decimal import Decimal, InvalidOperation
    from .adjust_factor import factor_for

    state = dict(state or {})
    if bar is None or bar.get('suspended') or events is None or factor_quality == 'UNPROCESSED':
        return None, state
    window = (list(state.get('window', [])) + [[str(bar['ts'])[:10], bar.get('close'),
                                                bar.get('turnover_pct')]])[-120:]
    adjusted, reasons = [], set()
    for day, close, _ in window:
        factor, quality = factor_for(events, day)
        if quality == 'NO_EVENTS':
            factor = 1.0
        elif quality == 'BEFORE_FIRST_EVENT':
            first = min(events, key=lambda item: item['dividOperateDate'])
            factor, _ = factor_for([first], first['dividOperateDate'])
        if quality != 'FORMAL':
            reasons.add('FACTOR_' + quality)
        if factor is None:
            return None, state
        try:
            adjusted.append(float(Decimal(str(close)) * Decimal(str(factor))))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            adjusted.append(None)
    return {'prices': adjusted, 'turnovers': [row[2] for row in window],
            'bars_used': len(window), 'factor_reasons': sorted(reasons)}, {'window': window}


STATE_JSON_CAPACITY = 12000
STATE_TIMESTAMP_US = 1794067200000000

class ChipPeakStore:
    """TD persistence; the supplied TaosStore owns connection and database."""

    def __init__(self, store):
        from .td_sink import validate_database_name
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        definitions = ', '.join(name + (' INT' if name in ('bars_used', 'dense_bins') else ' DOUBLE') for name in (('bars_used',) + FIELDS))
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.chip_indicators_v1 '
                        '(ts TIMESTAMP, version VARCHAR(24), algo_version VARCHAR(128), '
                        'input_refs NCHAR(12000), available_at VARCHAR(40), quality VARCHAR(16), '
                        f'warm BOOL, {definitions}) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.chip_window_state_v1 '
                        f'(ts TIMESTAMP, state_json VARCHAR({STATE_JSON_CAPACITY})) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.chip_peak_days '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), '
                        'business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), '
                        'available_at VARCHAR(40), quality VARCHAR(4000), missing_whitelist VARCHAR(4000))')

    def read_states(self, symbols):
        import json
        from .adjust_factor import AdjustFactorStore
        from .td_sink import _literal
        result = {}
        if symbols is None:
            sql = f'SELECT symbol, LAST_ROW(state_json) FROM {self.database}.chip_window_state_v1'
            rows, _ = self.store._day_scan(sql, suffix=' GROUP BY tbname, symbol', strict=True, has_where=False)
            return {str(symbol): json.loads(payload) for symbol, payload in rows}
        symbols = sorted({AdjustFactorStore._symbol(s) for s in symbols})
        for start in range(0, len(symbols), 200):
            selected = ', '.join(_literal(s) for s in symbols[start:start + 200])
            sql = (f'SELECT symbol, LAST_ROW(state_json) FROM {self.database}.chip_window_state_v1 '
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
                    raise ValueError('chip_window_state_json_exceeds_capacity')
                parts.append(f'{self.database}.chip_window_state_{symbol} USING '
                             f'{self.database}.chip_window_state_v1 TAGS({_literal(symbol)}) '
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
                                                    'available_at', 'quality', 'warm') + (('bars_used',) + FIELDS)]
                parts.append(f'{self.database}.chip_indicators_{symbol} USING '
                             f'{self.database}.chip_indicators_v1 TAGS({_literal(symbol)}) '
                             f'VALUES ({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')
            if parts:
                self.store._run('INSERT INTO ' + ' '.join(parts))

    def read_day_marker(self, day):
        from .td_sink import trade_day_ts_us
        rows = self.store._run(f'SELECT trade_date, version, business_status, row_count, sha256, '
                               f'available_at, quality, missing_whitelist FROM {self.database}.chip_peak_days '
                               f'WHERE ts = {trade_day_ts_us(day)}', missing_ok=True, strict=True)
        return rows[0] if rows else None

    def count_day_results(self, day):
        from .td_sink import trade_day_ts_us
        rows = self.store._run(f'SELECT COUNT(*) FROM {self.database}.chip_indicators_v1 '
                               f'WHERE ts = {trade_day_ts_us(day)}', strict=True)
        return int(rows[0][0])

    def write_day_marker(self, day, version, business_status, row_count, sha256, available_at, quality,
                         missing_whitelist='[]'):
        from .td_sink import _literal, trade_day_ts_us
        if len(quality) > 4000 or len(missing_whitelist) > 4000:
            raise ValueError('marker_too_large')
        values = [day, version, business_status, row_count, sha256, available_at, quality, missing_whitelist]
        self.store._run(f'INSERT INTO {self.database}.chip_peak_days VALUES '
                        f'({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')



def _calculate_day(day, bars, factors, states, version, available_at, detail):
    import json
    prepared, updates = {}, {}
    for symbol, source in sorted(bars.items()):
        if states.get(symbol, {}).get('last_trade_date', '') >= day:
            detail['skipped_already_processed'] += 1
            continue
        if source.get('suspended'):
            detail['skipped_no_bar'] += 1
            continue
        events, quality = factors.get(symbol, (None, 'UNPROCESSED'))
        if quality == 'UNPROCESSED' or events is None:
            detail['skipped_factor_unprocessed'] += 1
            continue
        window, state = prepare_window(dict(source, ts=day), events, states.get(symbol), factor_quality=quality)
        if window is None:
            detail['skipped_factor_unprocessed'] += 1
            continue
        state['last_trade_date'] = day
        prepared[symbol] = (source, window)
        updates[symbol] = state
    outputs = calculate_windows([w['prices'] for _, w in prepared.values()],
                                [w['turnovers'] for _, w in prepared.values()])
    rows = {}
    for (symbol, (source, window)), result in zip(prepared.items(), outputs, strict=True):
        reasons = list(window['factor_reasons'])
        if window['bars_used'] < 120:
            reasons.append('WARMUP_SHORT')
        if result['quality'] == 'PARTIAL' and not reasons:
            reasons.append('INVALID_WINDOW')
        if reasons:
            result['quality'] = 'PARTIAL'
            result['warm'] = False
        if result['quality'] == 'PARTIAL':
            detail['partial_total'] += 1
            for reason in reasons:
                detail['partial_reasons'][reason] = detail['partial_reasons'].get(reason, 0) + 1
        detail['warm_total'] += int(result['warm'])
        result.update(version=version, algo_version='chip_peak_v1', available_at=available_at,
                      input_refs=json.dumps({'daily_kline': source.get('version'),
                                             'window_start': updates[symbol]['window'][0][0],
                                             'window_end': day, 'bars_used': window['bars_used'],
                                             'partial_reasons': reasons}, separators=(',', ':')))
        rows[symbol] = result
    detail['computed'] += len(rows)
    return rows, updates


def _detail():
    return {'computed': 0, 'skipped_no_bar': 0, 'skipped_factor_unprocessed': 0,
            'skipped_already_processed': 0, 'partial_total': 0, 'partial_reasons': {},
            'warm_total': 0, 'elapsed_seconds': 0, 'aborted': False}


def run(request, root, *, store=None, clock=None, dry_run=False):
    """Build one trading day's indicators with batch reads and marker last."""
    import hashlib
    import json
    import time
    from datetime import date, datetime, timezone
    from .auction_amount_facts import _reply, _registered_failure, core_td_database
    from .td_sink import TaosStore, TdResourceExhausted, td_memory_exhausted
    clock = clock or time.monotonic
    started = clock()
    detail = _detail()
    try:
        day = request['trade_date']
        args = request.get('args', {})
        mode, from_day = args.get('mode', 'incremental'), args.get('from_date')
        if date.fromisoformat(day).isoformat() != day or mode not in ('incremental', 'rebuild'):
            raise ValueError('invalid date or mode')
        if from_day is not None and (date.fromisoformat(from_day).isoformat() != from_day or from_day > day or mode != 'rebuild'):
            raise ValueError('invalid from_date')
    except (KeyError, TypeError, ValueError) as exc:
        detail['message'] = str(exc)
        return _reply(64, 'CONFIG_ERROR', detail)
    if dry_run:
        return _reply(0, detail=detail)
    owned = store is None
    facts = ChipPeakStore(TaosStore(core_td_database())) if owned else store
    try:
        facts.ensure_schema()
        if mode == 'rebuild':
            return _rebuild(facts, owned, root, day, from_day, started, clock)
        if owned:
            from .adjust_factor import AdjustFactorStore
            bars = facts.store.read_day_rows(day)
            factors = AdjustFactorStore(facts.store).read_events_many(bars)
        else:
            bars = facts.read_day_rows(day)
            factors = facts.read_events_many(bars)
        states = facts.read_states(None)
        if not states:
            detail['hint'] = 'NEEDS_REBUILD'
        now = datetime.now(timezone.utc)
        version, available_at = now.strftime('%Y%m%d%H%M%S%f')[:20], now.isoformat()
        rows, updates = _calculate_day(day, bars, factors, states, version, available_at, detail)
        if rows:
            facts.write_results(day, rows)
            facts.write_states(updates)
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            detail['elapsed_seconds'] = round(clock() - started, 3)
            facts.write_day_marker(day, version, 'PARTIAL' if detail['partial_total'] or 'hint' in detail else 'FORMAL',
                                   facts.count_day_results(day), digest, available_at, json.dumps(detail, separators=(',', ':')))
        detail['elapsed_seconds'] = round(clock() - started, 3)
        return _reply(10 if 'hint' in detail else 0, detail=detail)
    except Exception as exc:
        detail['aborted'] = True
        detail['elapsed_seconds'] = round(clock() - started, 3)
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _registered_failure('TD_RESOURCE_EXHAUSTED', detail)
        raise
    finally:
        if owned:
            facts.store.close()
