"""Point-in-time inverse index of reference plate memberships."""
from __future__ import annotations

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


def build_index(rows):
    index = {}
    seen = set()
    for row in rows:
        symbol, code = row['symbol'], row['plate_code']
        if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol) or not isinstance(code, str) or not code:
            raise ValueError('invalid_plate_members_identity')
        if (symbol, code) in seen:
            raise ValueError('duplicate_plate_members_identity')
        seen.add((symbol, code))
        index.setdefault(symbol, []).append({key: row[key] for key in
                                            ('plate_code', 'plate_class', 'plate_member_label')})
    for plates in index.values():
        plates.sort(key=lambda item: item['plate_code'])
    return dict(sorted(index.items()))


def run(trade_date, asof, *, reference_store=None, store=None, redis_client=None):
    from .reference_snapshot_td import ReferenceSnapshotStore
    from .auction_redis_view import redis_client as make_redis
    from ..storage.two_layer import TwoLayerStore
    day = date.fromisoformat(trade_date).isoformat()
    cutoff = datetime.fromisoformat(asof.replace('Z', '+00:00'))
    if cutoff.tzinfo is None or cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != day:
        raise ValueError('invalid_asof')
    cutoff = cutoff.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
    ref = reference_store or ReferenceSnapshotStore()
    try:
        snapshot = ref.read_as_of('plate_members', cutoff)
    finally:
        if reference_store is None:
            ref.close()
    if not snapshot:
        raise ValueError('plate_members_unavailable')
    available = datetime.fromisoformat(snapshot['available_at'].replace('Z', '+00:00'))
    if available.tzinfo is None or available > datetime.fromisoformat(cutoff):
        raise ValueError('plate_members_not_visible')
    index = build_index(snapshot['rows'])
    rows = [{'symbol': symbol, 'plates': plates} for symbol, plates in index.items()]
    instant = datetime.fromisoformat(cutoff)
    epoch = datetime(1970, 1, 1, tzinfo=ZoneInfo('UTC'))
    delta = instant - epoch
    asof_id = format((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds, 'x')
    metadata = {'version': snapshot['version'] + ':' + asof_id, 'member_version': snapshot['version'],
                'member_available_at': available.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
                'available_at': cutoff, 'asof': cutoff}
    return (store or TwoLayerStore(redis_client or make_redis())).publish('plate_stock_index', day, rows, metadata)


def get_stock_plates(symbols, trade_date, *, store=None, redis_client=None, readonly=False, hot_only=False):
    from .auction_redis_view import redis_client as make_redis
    from ..storage.two_layer import TwoLayerStore
    selected = store or TwoLayerStore(redis_client or make_redis())
    if readonly:
        return read_stocks(selected, trade_date, symbols, restore=False, hot_only=hot_only)
    return selected.get_stock_plates(trade_date, symbols)


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _envelope(day, rows, metadata):
    import hashlib
    payload = {'dataset': 'plate_stock_index', 'trade_date': day, 'rows': rows,
               'row_count': len(rows), **metadata}
    raw = _json(payload).encode('utf-8')
    version = metadata['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
    if len(version) > 64:
        raise ValueError('archive_version_too_long')
    return payload, raw, version


def _write_view(redis, day, value):
    from .redis_day_view import write_day_view
    rows = {row['symbol']: {'plates': row['plates'], 'member_version': value['member_version']}
            for row in value['rows']}
    if len(rows) != value['row_count']:
        raise ValueError('duplicate_index_symbol')
    write_day_view(redis, 'plate_stock_index', day, rows, source='plate_members',
                   version=value['version'], available_at=value['available_at'],
                   fields=('plates', 'member_version'))


def publish(store, day, rows, metadata):
    from ..storage.two_layer import StoreResult
    from . import cold_archive
    try:
        value, raw, version = _envelope(day, rows, metadata)
        archive_status, reason = 'ARCHIVED', None
        try:
            receipt = cold_archive.archive('plate_stock_index', day, version, raw, value)
            if receipt is False or receipt is None:
                archive_status, reason = 'NOT_QUEUED', 'cold_archive_not_queued'
            elif not receipt.done.wait(6):
                archive_status, reason = 'UNKNOWN', 'cold_archive_timeout'
            elif not receipt.success:
                archive_status, reason = 'FAILED', receipt.error_category
        except Exception as exc:
            archive_status, reason = 'FAILED', type(exc).__name__
        _write_view(store.redis, day, value)
        return StoreResult(status='published', version=value['version'], metadata=value,
                           archive_status=archive_status, reason=reason)
    except Exception as exc:
        return StoreResult(status='FAILED', reason=type(exc).__name__)


def read_stocks(store, day, symbols, *, restore=True, hot_only=False):
    import json
    from . import cold_archive
    from .redis_day_view_read import read_day_view
    date.fromisoformat(day)
    symbols = list(dict.fromkeys(symbols))
    if any(not isinstance(s, str) or not re.fullmatch(r'[0-9]{6}', s) for s in symbols):
        raise ValueError('invalid_symbol')
    if not symbols:
        return {}

    class PipelinedRedis:
        def get(self, key):
            return store.redis.get(key)
        def hmget(self, key, batch):
            pipe = store.redis.pipeline()
            pipe.hmget(key, batch)
            return pipe.execute()[0]

    cold = None
    def load_cold():
        nonlocal cold
        cold = store.cold
        if cold is None:
            if not cold_archive._enabled():
                raise RuntimeError('cold_archive_disabled')
            cold = cold_archive.TdStore()
        version = cold.latest_version('plate_stock_index', day)
        if version is None:
            return None
        raw = cold_archive.read_envelope('plate_stock_index', day, version, store=cold)
        value = json.loads(raw)
        expected, canonical, computed = _envelope(day, value['rows'], {k: value[k] for k in
            ('version', 'member_version', 'member_available_at', 'available_at', 'asof')})
        if value != expected or canonical != raw or computed != version:
            raise ValueError('cold_index_integrity')
        return value

    try:
        try:
            view = read_day_view(PipelinedRedis(), 'plate_stock_index', day, symbols,
                                 td_loader=lambda *_: {}, ledger=lambda *_: None)
        except Exception:
            view = {'source': 'td_fallback', 'rows': {}, 'meta': {}}
        meta = view['meta'] if view['source'] == 'redis' else {}
        hot_rows = view['rows']
        if meta and len(hot_rows) == len(symbols):
            return {s: {'status': 'PRESENT_REDIS', 'plates': hot_rows[s]['plates'],
                        'version': meta['version']} for s in symbols}
        if hot_only:
            return {s: ({'status': 'PRESENT_REDIS', 'plates': hot_rows[s]['plates'], 'version': meta['version']}
                        if s in hot_rows and meta else {'status': 'MISSING' if meta else 'UNKNOWN', 'version': meta.get('version'),
                                                        'reason': 'hot_index_missing' if meta else 'hot_index_unavailable'}) for s in symbols}
        value = load_cold()
        if value is None:
            if meta:
                return {s: ({'status': 'PRESENT_REDIS', 'plates': hot_rows[s]['plates'], 'version': meta['version']}
                            if s in hot_rows else {'status': 'UNKNOWN', 'version': meta['version'],
                                                   'reason': 'cold_index_unavailable'}) for s in symbols}
            return {s: {'status': 'MISSING', 'version': None} for s in symbols}
        if meta and meta['version'] != value['version']:
            raise ValueError('cold_index_version_mismatch')
        index = {r['symbol']: r['plates'] for r in value['rows']}
        try:
            if restore:
                _write_view(store.redis, day, value)
        except Exception:
            pass
        return {s: ({'status': 'RESTORED_FROM_TD', 'plates': index[s], 'version': value['version']} if s in index
                    else {'status': 'MISSING', 'version': value['version']}) for s in symbols}
    except Exception as exc:
        if cold_archive.is_missing_table_error(exc):
            return {s: {'status': 'MISSING', 'version': None} for s in symbols}
        return {s: {'status': 'UNKNOWN', 'version': None, 'reason': type(exc).__name__} for s in symbols}
    finally:
        if cold is not None and store.cold is None:
            cold.close()


def publish_reference_view(redis, dataset, snapshot):
    """Publish a complete reference snapshot atomically as one Redis value."""
    if dataset not in ('plate_registry', 'stock_static'):
        raise ValueError('invalid_reference_dataset')
    rows = snapshot['rows']
    if not isinstance(rows, list) or not rows or not snapshot.get('version'):
        raise ValueError('invalid_reference_snapshot')
    stamp = datetime.fromisoformat(snapshot['available_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('invalid_reference_available_at')
    day = date.fromisoformat(snapshot['trade_date']).isoformat()
    payload = dict(snapshot, dataset=dataset, trade_date=day, row_count=len(rows))
    redis.set(f'core:view:{dataset}:{day}', _json(payload))


def read_reference_view(redis, dataset, day, asof):
    """Read a complete, point-in-time visible reference snapshot from Redis only."""
    import json
    if dataset not in ('plate_registry', 'stock_static'):
        raise ValueError('invalid_reference_dataset')
    date.fromisoformat(day)
    cutoff = datetime.fromisoformat(asof.replace('Z', '+00:00'))
    if cutoff.tzinfo is None:
        raise ValueError('invalid_asof')
    raw = redis.get(f'core:view:{dataset}:{day}')
    if raw is None:
        return None
    value = json.loads(raw)
    stamp = datetime.fromisoformat(value['available_at'].replace('Z', '+00:00'))
    if (stamp.tzinfo is None or stamp > cutoff or value.get('dataset') != dataset
            or value.get('trade_date', '') > day or not value.get('version')
            or not isinstance(value.get('rows'), list) or not value['rows']
            or value.get('row_count') != len(value['rows'])):
        return None
    return value


def preload_canonical_reference(trade_date, *, data_dir=None, redis_client=None, available_at=None):
    """Directly preload canonical plate registry and stock plate membership CSVs into Redis views."""
    import csv
    from pathlib import Path
    from .auction_redis_view import redis_client as make_redis
    from .redis_day_view import write_day_view

    day = date.fromisoformat(trade_date).isoformat()
    client = redis_client or make_redis()
    root = Path(data_dir) if data_dir else (Path(__file__).parent.parent.parent.parent / "plate/data")

    reg_file = root / "plate_registry.csv"
    mem_file = root / "stock_plate_membership.csv"
    if not reg_file.is_file() or not mem_file.is_file():
        raise FileNotFoundError(f"canonical_reference_missing:{reg_file}:{mem_file}")

    stamp = available_at or f"{day}T09:00:00+08:00"

    with open(reg_file, "r", encoding="utf-8") as f:
        reg_rows = list(csv.DictReader(f))
    for r in reg_rows:
        r["trade_date"] = day
        r["available_at"] = stamp
        if "size" in r and r["size"]:
            r["size"] = int(r["size"])

    publish_reference_view(client, "plate_registry", {
        "dataset": "plate_registry",
        "trade_date": day,
        "version": f"{day.replace('-', '')}090000-canonical",
        "available_at": stamp,
        "rows": reg_rows
    })

    classes_by_code = {r["plate_code"]: r.get("class", "THEME") for r in reg_rows}
    index = {}
    with open(mem_file, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            s = r["symbol"].strip().zfill(6)
            c = r["plate_code"].strip()
            if s and c:
                index.setdefault(s, []).append({
                    "plate_code": c,
                    "plate_class": classes_by_code.get(c, "THEME"),
                    "plate_member_label": ""
                })
    for plates in index.values():
        plates.sort(key=lambda item: item["plate_code"])

    rows = {s: {"plates": plates, "member_version": f"{day.replace('-', '')}090000-canonical"}
            for s, plates in sorted(index.items())}
    write_day_view(client, "plate_stock_index", day, rows, source="canonical_csv",
                   version=f"{day.replace('-', '')}090000-canonical", available_at=stamp,
                   fields=("plates", "member_version"))

    static_file = root / "stock_static.csv"
    static_rows = []
    if static_file.is_file():
        with open(static_file, "r", encoding="utf-8") as f:
            static_rows = list(csv.DictReader(f))
        for r in static_rows:
            r["trade_date"] = day
            r["available_at"] = stamp
            r["is_st"] = str(r.get("is_st", "")).lower() in ("true", "1")
        publish_reference_view(client, "stock_static", {
            "dataset": "stock_static",
            "trade_date": day,
            "version": f"{day.replace('-', '')}090000-canonical",
            "available_at": stamp,
            "rows": static_rows
        })

    return {
        "plate_registry_rows": len(reg_rows),
        "plate_stock_symbols": len(rows),
        "stock_static_rows": len(static_rows),
        "status": "READY"
    }


def verify_plate_reference(trade_date, *, redis_client=None, data_dir=None,
                           auto_heal: bool = False, min_coverage: float = 0.95,
                           min_rows: int = 1000):
    """Decoupled pure data function to inspect and optionally heal plate reference views."""
    import json
    from datetime import date
    from .auction_redis_view import redis_client as make_redis
    from .redis_day_view import read_day_view

    day = date.fromisoformat(trade_date).isoformat()
    client = redis_client or make_redis()
    healed = False

    def _inspect():
        reg_raw = client.get(f"core:view:plate_registry:{day}")
        reg_rows = []
        if reg_raw:
            try:
                reg_obj = json.loads(reg_raw) if isinstance(reg_raw, (str, bytes)) else reg_raw
                reg_rows = reg_obj.get("rows", [])
            except Exception:
                reg_rows = []

        try:
            idx_meta, idx_rows = read_day_view(client, "plate_stock_index", day)
        except Exception:
            idx_meta, idx_rows = None, None

        static_raw = client.get(f"core:view:stock_static:{day}")
        static_rows = []
        if static_raw:
            try:
                static_obj = json.loads(static_raw) if isinstance(static_raw, (str, bytes)) else static_raw
                static_rows = static_obj.get("rows", [])
            except Exception:
                static_rows = []

        return reg_rows, idx_rows, static_rows, bool(reg_raw and idx_meta)

    reg_rows, idx_rows, static_rows, both_present = _inspect()

    if (not both_present or len(reg_rows) < min_rows or not idx_rows or not static_rows) and auto_heal:
        try:
            preload_canonical_reference(day, data_dir=data_dir, redis_client=client)
            healed = True
            reg_rows, idx_rows, static_rows, both_present = _inspect()
        except Exception:
            pass

    plate_count = len(reg_rows)
    stock_count = len(idx_rows) if idx_rows else 0
    static_count = len(static_rows)
    unnamed_count = sum(1 for r in reg_rows if not r.get("name"))

    if not both_present and not plate_count and not stock_count:
        return {
            "status": "MISSING",
            "trade_date": day,
            "plate_count": 0,
            "stock_count": 0,
            "stock_static_count": 0,
            "unnamed_plate_count": 0,
            "coverage": 0.0,
            "healed": healed,
            "reasons": ["views_not_found"]
        }

    expected_stocks = 5000
    coverage = min(1.0, stock_count / expected_stocks) if stock_count else 0.0
    reasons = []
    if plate_count < min_rows:
        reasons.append(f"plate_count_below_min:{plate_count}<{min_rows}")
    if coverage < min_coverage:
        reasons.append(f"coverage_below_min:{coverage:.3f}<{min_coverage:.3f}")
    if unnamed_count > max(10, int(plate_count * 0.05)):
        reasons.append(f"unnamed_plates_excessive:{unnamed_count}")
    if not static_count:
        reasons.append("stock_static_missing")

    status = "OK" if not reasons else "DEGRADED"

    return {
        "status": status,
        "trade_date": day,
        "plate_count": plate_count,
        "stock_count": stock_count,
        "stock_static_count": static_count,
        "unnamed_plate_count": unnamed_count,
        "coverage": round(coverage, 4),
        "healed": healed,
        "reasons": reasons
    }

