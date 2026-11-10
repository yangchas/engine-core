"""Admission based on the host's available memory, before handler dispatch."""
import json
from pathlib import Path


_BUDGET = json.loads(Path(__file__).with_name('resource_budget.json').read_text(encoding='utf-8'))


def available_mb(meminfo_path='/proc/meminfo'):
    for line in Path(meminfo_path).read_text(encoding='utf-8').splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    raise ValueError('MemAvailable missing from meminfo')


def required_mb(request):
    args = request.get('args')
    if isinstance(args, dict) and 'min_available_mb' in args:
        return args['min_available_mb']
    return _BUDGET['min_available_mb'].get(request.get('cost'), 0)


def admit(request, *, reader=available_mb):
    required = required_mb(request)
    available = reader()
    info = {'available_mb': available, 'required_mb': required, 'cost': request.get('cost') or 'light'}
    return available >= required, info
