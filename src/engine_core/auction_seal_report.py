"""Read and summarize observable 09:25 auction order-book seals."""
from collections import defaultdict
from datetime import date
import hashlib
import json
from types import SimpleNamespace

from .data_pipeline import cold_archive, plate_stock_index


def build_seal_section(facts, stock_plates, *, top_n=10):
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 0:
        raise ValueError('invalid_top_n')
    missing = {}
    for key in ('unknown_listing', 'unknown_prices', 'unlimited'):
        symbols = sorted(facts[key])
        missing[key] = {'count': len(symbols), 'symbols': symbols[:20]}
    for key, predicate in (('seal_amount_yuan', lambda row: row['seal_amount_yuan'] is None),
                           ('basis', lambda row: not row['basis'])):
        symbols = sorted(row['symbol'] for row in facts['rows'] if predicate(row))
        missing[key] = {'count': len(symbols), 'symbols': symbols[:20]}
    result = {'status': 'available', 'missing': missing,
              'themes': {'status': 'available' if stock_plates is not None else 'unavailable'},
              'notes': ['一只股票属于多个题材时，各题材分别计入；涨停与跌停分开统计。']}
    for side in ('limit_up', 'limit_down'):
        side_rows = [r for r in facts['rows'] if r['side'] == side]
        estimated = [r for r in side_rows if r.get('price_source', 'missing') != 'auction_match']
        estimated.sort(key=lambda r: r['symbol'])
        rows = [r for r in side_rows if r.get('price_source', 'missing') == 'auction_match' and r['seal_amount_yuan'] is not None and r['basis']]
        rows.sort(key=lambda r: (-r['seal_amount_yuan'], r['symbol']))
        result[side] = {
            'confirmed': {'count': len(rows), 'seal_amount_yuan': sum(r['seal_amount_yuan'] for r in rows),
                          'top_stocks': rows[:top_n]},
            'estimated': {'count': len(estimated),
                          'seal_amount_yuan': (sum(r['seal_amount_yuan'] for r in estimated)
                                               if all(r['seal_amount_yuan'] is not None and r['basis'] for r in estimated) else None),
                          'rows': estimated},
        }
        if stock_plates is not None:
            themes = defaultdict(lambda: {'count': 0, 'seal_amount_yuan': 0})
            for row in rows:
                for plate in {p['plate_code']: p for p in stock_plates.get(row['symbol'], [])}.values():
                    entry = themes[plate['plate_code']]
                    entry.update(plate)
                    entry['count'] += 1
                    entry['seal_amount_yuan'] += row['seal_amount_yuan']
            result['themes'][side] = sorted(themes.values(), key=lambda p: (-p['seal_amount_yuan'], p['plate_code']))[:top_n]
    return result


def _validate(raw, day, archive_version=None):
    value = json.loads(raw)
    if (value['dataset'] != 'auction_seal_facts' or value['trade_date'] != day
            or not isinstance(value['rows'], list) or value['row_count'] != len(value['rows'])
            or any(not isinstance(value[k], list) for k in ('unknown_listing', 'unknown_prices', 'unlimited'))):
        raise ValueError('invalid_seal_envelope')
    if archive_version is not None:
        digest = hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest()[:16]
        if archive_version != value['version'] + ':' + digest:
            raise ValueError('seal_archive_version_mismatch')
    return value


def _read_facts(store, day):
    try:
        raw = store.redis.get(f'core:view:auction_seal_facts:{day}')
        if raw is not None:
            return _validate(raw, day)
    except Exception:
        pass
    cold = store.cold
    owned = False
    try:
        if cold is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            cold = cold_archive.TdStore()
            owned = True
        version = cold.latest_version('auction_seal_facts', day)
        if version is None:
            raise ValueError('seal_facts_missing')
        return _validate(cold_archive.read_envelope('auction_seal_facts', day, version, store=cold), day, version)
    finally:
        if owned:
            cold.close()


class _ReadOnlyRedis:
    def __init__(self, client):
        self.client = client

    def get(self, key):
        return self.client.get(key)

    def pipeline(self):
        return _ReadOnlyPipeline(self.client.pipeline())


class _ReadOnlyPipeline:
    def __init__(self, pipeline):
        self._pipeline = pipeline

    def hmget(self, key, symbols):
        self._pipeline.hmget(key, symbols)
        return self

    def execute(self):
        return self._pipeline.execute()


def read_and_build(trade_date, *, store=None, redis_client=None):
    try:
        day = date.fromisoformat(trade_date).isoformat()
        if store is None:
            from .data_pipeline.auction_redis_view import redis_client as make_redis
            from .storage.two_layer import TwoLayerStore
            store = TwoLayerStore(redis_client if redis_client is not None else make_redis())
        facts = _read_facts(store, day)
        symbols = list(dict.fromkeys(r['symbol'] for r in facts['rows']))
        mapping = None
        try:
            readonly_store = SimpleNamespace(redis=_ReadOnlyRedis(store.redis), cold=store.cold)
            indexed = plate_stock_index.get_stock_plates(symbols, day, store=readonly_store)
            versions = {item.get('version') for item in indexed.values()}
            if (set(indexed) == set(symbols) and len(versions) <= 1 and
                    all(item['status'] in ('PRESENT_REDIS', 'RESTORED_FROM_TD', 'MISSING') and item.get('version')
                        for item in indexed.values())):
                mapping = {symbol: item.get('plates', []) for symbol, item in indexed.items()}
        except Exception:
            pass
        return build_seal_section(facts, mapping)
    except Exception:
        return {'status': 'unavailable', 'themes': {'status': 'unavailable'}}
