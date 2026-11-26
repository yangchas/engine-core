"""Read local historical and capture JSON without inventing capture timestamps."""
import json
from datetime import datetime
from pathlib import Path

def load_kaipan_cache(path):
    path = Path(path)
    files = [path] if path.is_file() else sorted(path.rglob('*.json'))
    records = []
    for file in files:
        raw = json.loads(file.read_text(encoding='utf-8'))
        if not isinstance(raw, dict): continue
        if 'response' in raw and isinstance(raw['response'], dict):
            rows = raw['response'].get('list', [])
            plates = [{'code': str(r[0]), 'name': str(r[1]), 'raw_fields': r[2:]} for r in rows if isinstance(r,list) and len(r)>=2]
            records.append({'trade_date': str(raw.get('captured_local',''))[:10], 'available_at': raw.get('captured_local'), 'plates': plates, 'source': str(file)})
        elif 'his_plates' in raw:
            records.append({'trade_date': raw.get('trade_date', file.stem), 'available_at': raw.get('fetched_at'),
                            'his_plates': raw.get('his_plates'), 'his_bans': raw.get('his_bans'),
                            'his_zha': raw.get('his_zha'), 'his_floor': raw.get('his_floor'), 'source': str(file)})
    return records


def available_kaipan_plates(records, trade_date, cutoff):
    """Return the latest dated plate observation demonstrably available by cutoff.

    Historical apphis responses fetched later are retrospective data, even if
    their trade date precedes the cutoff.  Unknown or timezone-incompatible
    availability is never treated as an observation at the market close.
    """
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    eligible = []
    for record in records:
        if record.get('trade_date') != trade_date or not record.get('available_at'):
            continue
        try:
            available = datetime.fromisoformat(record['available_at'])
            if available.tzinfo != cutoff.tzinfo and (available.utcoffset() is None or cutoff.utcoffset() is None):
                continue
            if available > cutoff:
                continue
        except (TypeError, ValueError):
            continue
        plates = record.get('plates')
        if plates is None:
            response = record.get('his_plates')
            data = response.get('data', {}) if isinstance(response, dict) and response.get('ok') else {}
            rows = data.get('list') if isinstance(data, dict) else None
            if isinstance(rows, list):
                plates = [{'code': str(row[0]), 'name': str(row[1]), 'raw_fields': row[2:]}
                          for row in rows if isinstance(row, list) and len(row) >= 2]
        if plates is not None:
            eligible.append((available, plates))
    return max(eligible, key=lambda item: item[0])[1] if eligible else None
