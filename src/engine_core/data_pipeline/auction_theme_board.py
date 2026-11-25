"""Pure auction theme board assembly; caller supplies all data and visibility metadata."""
from collections import Counter
from datetime import datetime
import hashlib
import json

from engine_core.auction_active_buckets import BUCKET_VERSION, build_active_bucket_view
from engine_core.auction_stock_themes import THEME_VERSION, DRIFT_VERSION, assign_stock_themes
from engine_core.auction_bucket_amount import AMOUNT_CONTEXT_VERSION, add_bucket_amount_context, render_bucket_board_md
from engine_core.auction_bucket_digest import build_anomaly_digest, render_digest_md


def _visible(item, asof):
    try:
        return bool(item and item.get('available_at') and datetime.fromisoformat(item['available_at']) <= asof)
    except (TypeError, ValueError):
        return False


def assemble(trade_date, asof, inputs):
    """Assemble supplied auction_0925 (rows/meta), auction_0924_meta (rows),
    prior_0925_by_date, plate_map (rows/available_at), caps (rows/available_at/
    trade_date/replay, optional), yesterday_pool, yesterday_reasons, reason_history,
    seal (sealed_symbols/available_at, optional), names, listings, plate_names,
    and trading_dates. No external reads or writes are performed.
    """
    def fail(reason, retryable=False):
        return {'status': 'failed', 'reason': reason, 'retryable': retryable, 'version': None, 'summary': {}}
    when = datetime.fromisoformat(asof)
    if when < datetime.fromisoformat(trade_date + 'T09:25:20+08:00'):
        return fail('asof_too_early', True)
    auction = inputs['auction_0925']
    meta = auction.get('meta') or {}
    if meta.get('status') != 'COMPLETE' or not meta.get('sha256'):
        return fail('auction_view_invalid')
    rows = auction['rows']
    if len(rows) < inputs['auction_0924_meta']['rows'] * .9:
        return fail('auction_view_incomplete', True)
    if not _visible(inputs.get('plate_map'), when):
        return fail('plate_map_not_visible')
    if inputs.get('yesterday_pool') is None:
        return fail('yesterday_pool_missing')
    mapping = inputs['plate_map']['rows']
    cap_input = inputs.get('caps')
    replay = bool(cap_input and cap_input.get('replay'))
    previous_dates = [d for d in inputs['trading_dates'] if d < trade_date]
    previous_date = max(previous_dates) if previous_dates else None
    cap_date_ok = cap_input and cap_input.get('trade_date') == previous_date
    caps = cap_input.get('rows') if cap_input and (replay or (cap_date_ok and _visible(cap_input, when))) else None
    seal = inputs.get('seal')
    seal_visible = _visible(seal, when)
    sealed = seal.get('sealed_symbols', ()) if seal_visible else ()
    pool = inputs['yesterday_pool']
    symbols = {r['symbol'] if isinstance(r, dict) else r for r in pool}
    counts = Counter(str(p.get('plate_code') if isinstance(p, dict) else p) for ps in mapping.values() for p in ps)
    active = build_active_bucket_view(rows, mapping, symbols, inputs['yesterday_reasons'], names=inputs['names'], plate_names=inputs['plate_names'], caps=caps, trade_date=trade_date, listings=inputs['listings'], trading_dates=inputs['trading_dates'], sealed_symbols=sealed)
    view = assign_stock_themes(active['stocks'], mapping, inputs['plate_names'], counts, inputs['reason_history'], caps=caps, limit_available=True)
    view = add_bucket_amount_context(view, rows, inputs['prior_0925_by_date'], mapping)
    digest = render_digest_md(build_anomaly_digest(view))
    board = render_bucket_board_md(view)
    if len(inputs['prior_0925_by_date']) < 2:
        digest = digest.replace('市场：', '市场（前几日不足）：', 1)
        board = board.replace('市场：', '市场（前几日不足）：', 1)
    amounts = [s['amount_yuan'] for s in active['stocks'] if s['amount_yuan'] is not None]
    denominator = active['market_amount_yuan']
    summary = {'bucket_version': BUCKET_VERSION, 'theme_version': THEME_VERSION, 'drift_version': DRIFT_VERSION, 'amount_context_version': AMOUNT_CONTEXT_VERSION,
               'arm_counts': {k: sum(k in s['sources'] for s in active['stocks']) for k in 'yacs'}, 'union_count': len(active['stocks']),
               'amount_share': sum(amounts) / denominator if denominator and len(amounts) == len(active['stocks']) else None, 'bucket_count': len(view['buckets']),
               'effective_bucket_count': sum(b['active']['count'] >= 3 for b in view['buckets']), 'market_ratio': view['market']['ratio'],
               'caps_unavailable': not bool(caps), 'replay_non_pit': replay, 'sealed_missing': not seal_visible}
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fields = {'digest_md': digest, 'board_md': board, 'summary_json': encode(summary), 'buckets_json': encode(view['buckets'])}
    version = hashlib.sha256(encode(fields).encode()).hexdigest()[:16]
    return {'status': 'ok', 'reason': None, 'retryable': False, 'version': version, 'fields': fields, 'summary': summary}
