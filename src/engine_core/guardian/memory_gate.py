"""Admission based on the host's available memory, before handler dispatch."""
import json
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path


_BUDGET = json.loads(Path(__file__).with_name('resource_budget.json').read_text(encoding='utf-8'))
_SCHEDULE_PATH = Path(__file__).with_name('schedule.json')


@lru_cache(maxsize=1)
def _schedule_costs(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return {event['event_id']: event['cost'] for event in data['events']
                if 'event_id' in event and 'cost' in event}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def available_mb(meminfo_path='/proc/meminfo'):
    for line in Path(meminfo_path).read_text(encoding='utf-8').splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    raise ValueError('MemAvailable missing from meminfo')


def _resolve(request):
    args = request.get('args')
    if isinstance(args, dict) and 'min_available_mb' in args:
        return args['min_available_mb'], request.get('cost') or 'light', 'args'
    cost = request.get('cost')
    if cost is not None:
        return _BUDGET['min_available_mb'].get(cost, 0), cost, 'request'
    event = request.get('event')
    instance = request.get('instance')
    item = request.get('item_id')
    candidates = [instance, event, f'fetch_item:{item}' if event == 'fetch_item' and item else None]
    for pattern, cost in _BUDGET.get('event_cost_overrides', {}).items():
        if any(isinstance(value, str) and fnmatchcase(value, pattern) for value in candidates):
            return _BUDGET['min_available_mb'].get(cost, 0), cost, 'override'
    cost = _schedule_costs(_SCHEDULE_PATH).get(event)
    if cost is not None:
        return _BUDGET['min_available_mb'].get(cost, 0), cost, 'schedule'
    return 0, 'light', 'default'


def required_mb(request):
    return _resolve(request)[0]


def admit(request, *, reader=available_mb):
    required, cost, source = _resolve(request)
    available = reader()
    info = {'available_mb': available, 'required_mb': required, 'cost': cost, 'cost_source': source}
    return available >= required, info
