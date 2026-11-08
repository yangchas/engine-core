import json, math
from pathlib import Path
p=Path(__file__).parent
rows=[json.loads(x) for x in (p/'daily_kline.jsonl').read_text().splitlines()]; expected=[json.loads(x) for x in (p/'expected.jsonl').read_text().splitlines()]; events=json.loads((p/'factor_events.json').read_text()); checks=json.loads((p/'checks.json').read_text())
assert len(rows)==len(expected)==847
assert all((r['symbol'],str(r['ts'])[:10])==(e['symbol'],e['trade_date']) for r,e in zip(rows,expected))
assert len(events)==8 and all(v is not None for v in events.values())
assert all(all(isinstance(e[k],str) for k in ('dividOperateDate','foreAdjustFactor','backAdjustFactor','adjustFactor')) for group in events.values() for e in group)
assert [x['product'] for x in checks]==['13.91999355','13.49000000']
assert all(all(v is None or not isinstance(v,float) or math.isfinite(v) for v in e.values()) for e in expected)
print('PASS daily and expected rows:',len(rows));print('PASS symbols with factor response:',len(events));print('PASS 000815 products:',*[x['product'] for x in checks]);print('PASS finite outputs and matching row keys')
