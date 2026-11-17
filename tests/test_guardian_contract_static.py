"""Every literal `return <exit code>, {"contract": 1, ...}` in the guardian code obeys the reply contract.

Replies built by helpers or from variables are not visible here; the dry-run audit (examples/audit_handler_contract.py)
and the observe-only flag in guardian_handler cover those at run time.
"""
import ast
from pathlib import Path

from engine_core.guardian.contract import check_reply

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [*sorted((ROOT / "src" / "engine_core" / "guardian").glob("*.py")),
           *sorted((ROOT / "src" / "engine_core" / "data_pipeline").glob("*.py")),
           *sorted((ROOT / "examples").glob("guardian*.py"))]
# Fields filled from variables are replaced by values that satisfy the rule for that exit code, so only literal
# mistakes are reported.
_DEFAULTS = {0: ("ok", ""), 10: ("degraded", "X"), 20: ("not_applicable", "ALREADY_SATISFIED"),
             30: ("failed", "X"), 40: ("failed", "X"), 64: ("failed", "CONFIG_ERROR"), 75: ("failed", "X")}


def _literal(node):
    try:
        return ast.literal_eval(node), True
    except (ValueError, TypeError, SyntaxError):
        return None, False


def literal_replies():
    for path in SCANNED:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2):
                continue
            code, ok = _literal(node.value.elts[0])
            body = node.value.elts[1]
            if not (ok and isinstance(code, int) and isinstance(body, ast.Dict)):
                continue
            reply = {}
            for key, value in zip(body.keys, body.values):
                name, key_ok = _literal(key) if key is not None else (None, False)
                if key_ok and isinstance(name, str):
                    reply[name], value_ok = _literal(value)
                    if not value_ok:
                        reply[name] = "<dynamic>"
            if reply.get("contract") == 1:
                status, error_code = _DEFAULTS.get(code, ("failed", "X"))
                for field, fallback in (("status", status), ("error_code", error_code),
                                        ("retryable", code in (30, 75))):
                    if reply.get(field) == "<dynamic>" or field not in reply:
                        reply[field] = fallback
                yield f"{path.relative_to(ROOT)}:{node.lineno}", code, reply


def test_the_scan_finds_replies():
    assert len(list(literal_replies())) > 30


def test_literal_guardian_replies_obey_the_contract():
    bad = [(where, code, check_reply(code, reply)) for where, code, reply in literal_replies()
           if check_reply(code, reply)]
    assert bad == []


def test_stock_cap_schedule_avoids_wencai_windows():
    import json
    from datetime import datetime, timedelta
    schedule = json.loads((ROOT / 'src/engine_core/guardian/schedule.json').read_text())
    events = schedule['events']
    cap, = [e for e in events if (e.get('args') or {}).get('dataset') == 'stock_cap']
    clock = lambda value: datetime.strptime(value, '%H:%M:%S')
    start, end = clock(cap['at']), clock(cap['deadline'])
    assert start > clock('18:30:00') and end <= start + timedelta(minutes=90)
    assert cap['calendar'] == 'trading_day' and cap['handler'] == 'intraday_data'
    for event in events:
        if event is cap: continue
        profile = schedule['profiles'].get(event.get('profile'), {})
        chain = (event.get('args') or {}).get('chain')
        is_wencai = ('wencai' in str(event).lower() or 'wencai' in str(profile).lower()
                     or isinstance(chain, dict) and chain.get('name') == 'daily_kline')
        if is_wencai:
            other_start, other_end = clock(event['at']), clock(event.get('late_until') or event['deadline'])
            timeout = max(event.get('timeout_seconds') or 0,
                          1500 if event.get('profile') == 'chain_sweep' else 0,
                          chain.get('budget_seconds', 0) if isinstance(chain, dict) else 0)
            other_end += timedelta(seconds=timeout)
            assert end <= other_start or start >= other_end, event['event_id']
