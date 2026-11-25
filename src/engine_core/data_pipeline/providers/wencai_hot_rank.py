"""Parse dated WenCai hot-rank responses without inventing snapshot times."""
from datetime import date
from typing import Any, Mapping

from .wencai_daily_adapter import DEFAULT_COOKIE_FILE


def normalize(payload: Mapping[str, Any], trade_date: str) -> list[dict[str, Any]]:
    day = date.fromisoformat(trade_date).isoformat()
    suffix = day.replace('-', '')
    try:
        components = payload['answer']['components']
        tables = [c['data']['datas'] for c in components
                  if isinstance(c.get('data'), Mapping) and isinstance(c['data'].get('datas'), list)]
    except (KeyError, TypeError, AttributeError):
        raise ValueError('wencai_hot_rank_invalid_payload') from None
    if str(payload.get('status_code')) != '0' or len(tables) != 1:
        raise ValueError('wencai_hot_rank_invalid_payload')
    result = []
    for item in tables[0]:
        try:
            code = item['股票代码']
            symbol = item['code']
            name = item['股票简称']
            rank = item[f'个股热度排名[{suffix}]']
            heat = item[f'个股热度[{suffix}]']
            if (not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit()
                    or not isinstance(code, str) or not code.startswith(symbol + '.')
                    or not isinstance(name, str) or not name or type(rank) is not int
                    or heat is None):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError('wencai_hot_rank_invalid_row_or_date') from None
        result.append(dict(symbol=symbol, code=code, name=name, rank=rank,
                           heat_raw=heat, source='wencai_hot_rank', trade_date=day))
    if len(result) != 100 or sorted(r['rank'] for r in result) != list(range(1, 101)):
        raise ValueError('wencai_hot_rank_incomplete')
    return sorted(result, key=lambda row: row['rank'])


def fetch(trade_date: str, *, cookie_file=DEFAULT_COOKIE_FILE, query_fn=None):
    """Fetch one dated top-100 response through the shared WenCai client."""
    import time
    from .wencai_daily_adapter import _Run, _read_cookie

    day = date.fromisoformat(trade_date).isoformat()
    if query_fn is None:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
    question = f"{day}个股热度排名前100"
    cookie = _read_cookie(cookie_file)
    run = _Run(query_fn=query_fn, context=None, page_size=100, cookie=cookie,
               cooldown_seconds=60, cooldown_factor=2, cooldown_max_seconds=300,
               max_cooldowns=0, max_requests=1, sleep_fn=time.sleep, clock_fn=time.monotonic,
               wall_budget_seconds=None, checkpoint_path=None, trade_date=day, question=question)
    result = run.call(question, 1)
    return normalize(result.get("payload", result), day)
