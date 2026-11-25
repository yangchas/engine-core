"""Stateless daily DDJE to turnover ratio."""
import hashlib
import json
import math
import time
from datetime import date, datetime, timezone

from .auction_amount_facts import _reply, _registered_failure, core_td_database
from .td_sink import TaosStore, TdResourceExhausted, td_memory_exhausted


def ratio(ddje, amount):
    if amount is None:
        return None, 'PARTIAL', 'DENOM_NULL'
    if ddje is None:
        return None, 'PARTIAL', 'DDJE_NULL'
    ddje, amount = float(ddje), float(amount)
    if not math.isfinite(ddje) or not math.isfinite(amount):
        return None, 'PARTIAL', 'NON_FINITE'
    if amount == 0:
        return None, 'PARTIAL', 'DENOM_ZERO'
    value = ddje / amount
    return (value, 'FORMAL', None) if math.isfinite(value) else (None, 'PARTIAL', 'NON_FINITE')


class DdeDerivedStore:
    def __init__(self, store):
        from .td_sink import validate_database_name
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.dde_indicators_v1 '
                        '(ts TIMESTAMP, ddje_ratio DOUBLE, version VARCHAR(24), algo_version VARCHAR(32), '
                        'input_refs VARCHAR(4096), available_at VARCHAR(40), quality VARCHAR(16)) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.dde_indicators_days '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), business_status VARCHAR(16), '
                        'row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), quality VARCHAR(4000), '
                        'missing_whitelist VARCHAR(4000))')

    def read_dde(self, day):
        from .td_sink import trade_day_ts_us
        sql = f'SELECT symbol, ddje, version FROM {self.database}.daily_dde_v1 WHERE ts = {trade_day_ts_us(day)}'
        rows, _ = self.store._day_scan(sql, strict=True)
        return {str(symbol): {'ddje': ddje, 'version': version} for symbol, ddje, version in rows}

    def read_day_rows(self, day):
        return self.store.read_day_rows(day)

    def write_results(self, day, rows):
        from .adjust_factor import AdjustFactorStore
        from .td_sink import INSERT_CHUNK_ROWS, _literal, trade_day_ts_us
        items = sorted(rows.items())
        for start in range(0, len(items), INSERT_CHUNK_ROWS):
            parts = []
            for symbol, row in items[start:start + INSERT_CHUNK_ROWS]:
                symbol = AdjustFactorStore._symbol(symbol)
                values = [row[k] for k in ('ddje_ratio', 'version', 'algo_version', 'input_refs', 'available_at', 'quality')]
                parts.append(f'{self.database}.dde_indicators_{symbol} USING {self.database}.dde_indicators_v1 '
                             f'TAGS({_literal(symbol)}) VALUES ({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')
            if parts:
                self.store._run('INSERT INTO ' + ' '.join(parts))

    def write_day_marker(self, day, version, quality, row_count, digest, available_at):
        from .td_sink import _literal, trade_day_ts_us
        values = [day, version, quality, row_count, digest, available_at, quality, '[]']
        self.store._run(f'INSERT INTO {self.database}.dde_indicators_days VALUES '
                        f'({trade_day_ts_us(day)}, ' + ', '.join(map(_literal, values)) + ')')


def run(request, root, *, store=None, dry_run=False):
    started = time.monotonic()
    detail = dict(computed=0, partial_total=0, partial_reasons={}, no_dde_row=0, no_kline_row=0,
                  elapsed_seconds=0, aborted=False, listing_source='fallback')
    try:
        day = request['trade_date']
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid trade date')
        from_day = request.get('args', {}).get('from_date')
        if from_day is not None and (date.fromisoformat(from_day).isoformat() != from_day or from_day > day):
            raise ValueError('invalid from_date')
    except (KeyError, TypeError, ValueError) as exc:
        detail['message'] = str(exc)
        return _reply(64, 'CONFIG_ERROR', detail)
    if dry_run:
        return _reply(0, detail=detail)
    owned = store is None
    facts = store if store is not None else DdeDerivedStore(TaosStore(core_td_database()))
    try:
        facts.ensure_schema()
        if from_day:
            if owned:
                from .trade_calendar import TradeCalendarStore
                days = TradeCalendarStore(root).trading_dates(from_day, day)
            else:
                days = [d for d in facts.trading_dates(day) if from_day <= d <= day]
        else:
            days = [day]
        listing = None
        snapshot = None
        try:
            if owned:
                from .reference_snapshot_td import ReferenceSnapshotStore
                from .stock_listing import read_active
                snapshot = ReferenceSnapshotStore()
                listing = read_active(snapshot)
            elif hasattr(facts, 'read_listing'):
                listing = facts.read_listing()
            if not listing or not listing.get('rows'):
                listing = None
        except Exception as exc:
            if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
                raise
            listing = None
        finally:
            if snapshot is not None:
                snapshot.close()
        if listing:
            detail['listing_source'] = 'snapshot'
        empty = False
        for current in sorted(days):
            dde, bars = facts.read_dde(current), facts.read_day_rows(current)
            if not dde and not bars:
                empty = True
                continue
            active = None
            if listing:
                from .stock_listing import listed_on
                active = {s.rsplit('.', 1)[-1].zfill(6) for s in listed_on(listing['rows'], current)}
            now = datetime.now(timezone.utc)
            available = now.isoformat()
            version = now.strftime('%Y%m%d%H%M%S%f')[:20]
            rows = {}
            for symbol in sorted(set(dde) | set(bars)):
                d, b = dde.get(symbol), bars.get(symbol)
                if d is None and (b.get('suspended') or (active is not None and symbol not in active)):
                    continue
                if d is None:
                    value, quality, reason = None, 'PARTIAL', 'NO_DDE_ROW'
                    detail['no_dde_row'] += 1
                elif b is None:
                    value, quality, reason = None, 'PARTIAL', 'NO_KLINE_ROW'
                    detail['no_kline_row'] += 1
                else:
                    value, quality, reason = ratio(d.get('ddje'), b.get('amount_yuan'))
                if reason:
                    detail['partial_total'] += 1
                    reasons = detail['partial_reasons']
                    reasons[reason] = reasons.get(reason, 0) + 1
                refs = {'daily_dde': {'date': current, 'version': d.get('version') if d else None},
                        'daily_kline': {'date': current, 'version': b.get('version') if b else None}, 'reason': reason}
                rows[symbol] = dict(ddje_ratio=value, quality=quality, version=version, algo_version='dde_derived_v1',
                                    input_refs=json.dumps(refs, separators=(',', ':')), available_at=available)
            facts.write_results(current, rows)
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            quality = 'PARTIAL' if any(r['quality'] == 'PARTIAL' for r in rows.values()) else 'FORMAL'
            facts.write_day_marker(current, version, quality, len(rows), digest, available)
            detail['computed'] += len(rows)
        detail['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if empty:
            code, reply = _reply(10, detail=detail)
            reply['reason'] = 'NO_INPUT_ROWS'
            return code, reply
        return _reply(0, detail=detail)
    except Exception as exc:
        detail['aborted'] = True
        detail['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _registered_failure('TD_RESOURCE_EXHAUSTED', detail)
        raise
    finally:
        if owned:
            facts.store.close()
