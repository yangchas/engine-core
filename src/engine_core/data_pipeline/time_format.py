"""Timezone-aware timestamp formatting for persisted data."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo('Asia/Shanghai')


def _datetime(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone_required')
    return result.astimezone(_SHANGHAI)


def shanghai_seconds(value):
    if value is None:
        return None
    return _datetime(value).isoformat(timespec='seconds')


def shanghai_seconds_ceil(value):
    if value is None:
        return None
    result = _datetime(value)
    if result.microsecond:
        result = result.replace(microsecond=0) + timedelta(seconds=1)
    return result.isoformat(timespec='seconds')
