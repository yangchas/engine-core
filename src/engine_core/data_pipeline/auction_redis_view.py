"""Read C977 full auction candidate strings and validate their publication pair."""
from __future__ import annotations

import hashlib
import json
import os
import re

TAGS = ('0920', '0924', '0925')
CUTOFFS = {'0920': '09:20:03', '0924': '09:24:10', '0925': '09:25:06'}
FIELDS = {'price_milli', 'pre_close_milli', 'change_bp', 'amount_yuan',
          'bid_amount_yuan', 'rank_amount', 'src'}


class ViewError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ViewError('duplicate_key')
        result[key] = value
    return result


def read_auction_view(client, day: str, tag: str, *, prefix: str = 'core:auction:full'):
    """Return rows only after atomic pair, byte digest and contract validation."""
    if tag not in TAGS:
        raise ValueError('invalid_tag')
    key = f'{prefix}:{day}:{tag}'
    try:
        payload, meta_bytes = client.mget([key, key + ':meta'])
    except Exception as exc:
        raise ViewError('redis_error') from exc
    if payload is None or meta_bytes is None:
        raise ViewError('missing_key')
    if not isinstance(payload, bytes) or not isinstance(meta_bytes, bytes):
        raise ViewError('invalid_bytes')
    try:
        meta = json.loads(meta_bytes, object_pairs_hook=_object)
        if not isinstance(meta, dict):
            raise ViewError('invalid_meta')
        if meta.get('status') != 'COMPLETE':
            raise ViewError('incomplete')
        if not isinstance(meta.get('sha256'), str) or hashlib.sha256(payload).hexdigest() != meta['sha256']:
            raise ViewError('digest_mismatch')
        data = json.loads(payload, object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewError('invalid_json') from exc
    if not isinstance(data, dict) or type(meta.get('rows')) is not int or len(data) != meta['rows']:
        raise ViewError('invalid_structure')
    if not data and tag == '0925':
        raise ViewError('empty_view')
    result = []
    for symbol, record in data.items():
        if not re.fullmatch(r'[0-9]{6}', symbol) or not isinstance(record, dict) or set(record) != FIELDS:
            raise ViewError('invalid_structure')
        for field in FIELDS - {'src'}:
            value = record[field]
            if value is None and field in {'price_milli', 'pre_close_milli', 'change_bp'}:
                continue
            if type(value) is not int or (field != 'change_bp' and value < 0):
                raise ViewError('invalid_field')
        src = record['src']
        if src not in ('auction_match', 'indicative_bid_ask_mid', 'missing'):
            raise ViewError('invalid_field')
        if (src == 'missing') != (record['price_milli'] is None):
            raise ViewError('invalid_field')
        if record['rank_amount'] < 1 or (record['price_milli'] is not None and record['price_milli'] <= 0):
            raise ViewError('invalid_field')
        result.append({**record, 'symbol': symbol, 'auction_tag': tag,
                       'auction_amount_yuan': record['amount_yuan'],
                       'source_rank_amount': record['rank_amount'],
                       'snapshot_cutoff': f'{day}T{CUTOFFS[tag]}+08:00',
                       'source': 'redis_auction_full'})
    return result


def redis_client():
    import redis
    return redis.Redis.from_url(os.environ.get('REDIS_URL', 'redis://127.0.0.1:6380/0'),
                                socket_connect_timeout=2, socket_timeout=3)
