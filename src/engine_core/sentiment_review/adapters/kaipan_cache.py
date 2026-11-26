"""Read local historical and capture JSON without inventing capture timestamps."""
import json
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
