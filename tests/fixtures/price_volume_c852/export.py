import json, math, hashlib, time, datetime, collections
from pathlib import Path
from decimal import Decimal
from engine_core.data_pipeline.td_sink import connect_td
p=Path('/home/exedev/validation/acceptance/289f271/c852'); start=time.monotonic(); now=datetime.datetime.now(datetime.timezone.utc).isoformat(); syms=['600519','000815','300750','688796','001381','002623','301139','000001']
def dump(name,obj):
 with (p/name).open('x') as f: json.dump(obj,f,ensure_ascii=False,indent=2,default=str);f.write('\n')
def jsonl(name,rows):
 with (p/name).open('x') as f:
  for x in rows:f.write(json.dumps(x,ensure_ascii=False,default=str,allow_nan=False)+'\n')
def query(q):
 cur.execute(q); return [dict(zip([x[0] for x in cur.description],r)) for r in cur.fetchall()]
c=connect_td();cur=c.cursor(); schema=query('DESCRIBE core_data.daily_kline_v1'); dump('schema.json',schema)
rows=[]; snap={}; queries=[]
for s in syms:
 q=f"SELECT * FROM core_data.daily_kline_v1 WHERE symbol = '{s}' ORDER BY ts"; queries.append(q); rows+=query(q)
 q=f"SELECT * FROM core_data.adjust_factor_v1 WHERE symbol = '{s}' ORDER BY ts DESC LIMIT 1"; queries.append(q); a=query(q)
 if a:snap[s]=a[0]
q='SELECT * FROM core_data.adjust_factor_days'; queries.append(q); markers=query(q)
cur.close();c.close();jsonl('daily_kline.jsonl',rows);dump('factor_snapshots.json',snap);dump('factor_days.json',markers)
raw={}
for fn in ['baostock_raw.json','baostock_extra_raw.json']:
 path=Path('/home/exedev/validation/acceptance/3380b2e/c846')/fn
 for item in json.loads(path.read_text()):
  s=item['code'].split('.')[-1]
  if s in syms:raw[s]={'source':str(path),'fields':item['fields'],'rows':item['rows']}
path=Path('/home/exedev/validation/acceptance/289f271/c851/baostock_000815.json'); raw['000815']={'source':str(path),'objects':json.loads(path.read_text())}
requests=[]; missing=[s for s in syms if s not in raw and s not in snap]
if missing:
 import baostock as bs
 try:
  login=bs.login();requests.append({'action':'login','error_code':login.error_code,'error_msg':login.error_msg})
  if login.error_code=='0':
   for s in missing:
    time.sleep(1.05);code=('sh.' if s.startswith('6') else 'sz.')+s
    rs=bs.query_adjust_factor(code=code,start_date='1990-01-01',end_date='2026-11-08'); log={'code':code,'error_code':rs.error_code,'error_msg':rs.error_msg,'time_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()};requests.append(log)
    if rs.error_code!='0':break
    data=[]
    while rs.next(): data.append(rs.get_row_data())
    if rs.error_code!='0':log['iteration_error']=rs.error_msg;break
    raw[s]={'source':'C852 Baostock query_adjust_factor 1990-01-01..2026-11-08','fields':rs.fields,'rows':data}
 except Exception as e: requests.append({'exception':type(e).__name__,'message':str(e)[:200]})
 finally:
  try:bs.logout()
  except Exception:pass
dump('baostock_responses.json',raw);dump('request_log.json',requests)
events={}; sources={}
for s in syms:
 if s in raw:
  item=raw[s]; events[s]=item.get('objects') if 'objects' in item else [dict(zip(item['fields'],r)) for r in item['rows']];sources[s]=item['source']
 elif s in snap:
  entries=json.loads(snap[s]['events_json']);events[s]=[{'dividOperateDate':x['date'],'foreAdjustFactor':x['fore'],'backAdjustFactor':x['back'],'adjustFactor':x['adjust']} for x in entries]; sources[s]='TD production snapshot'
 else:events[s]=None;sources[s]='UNKNOWN: no snapshot or successful response'
dump('factor_events.json',events)
calendar=json.loads(Path('/home/exedev/core-data/trade_calendar/calendar.json').read_text()); days=[x['d'] for x in calendar['days'] if x['t']==1]; nextday=dict(zip(days,days[1:])); by=collections.defaultdict(list)
for r in rows:by[r['symbol']].append(r)
expected=[]; checks=[]
for s in syms:
 hist=[]; e12=e26=dea=k=d=None; prev=None; ev=events[s]
 for r in by[s]:
  date=str(r['ts'])[:10]; candidates=[x for x in (ev or []) if x['dividOperateDate']<=date]; factor=Decimal(max(candidates,key=lambda x:x['dividOperateDate'])['foreAdjustFactor']) if candidates else None
  if prev and nextday.get(prev)!=date:hist=[];e12=e26=dea=k=d=None
  prev=date; vals=[]
  for field in ('close','high','low'):
   v=r[field]; vals.append(float(Decimal(str(v))*factor) if v is not None and factor is not None and not r['suspended'] else None)
  close,high,low=vals; out={'symbol':s,'trade_date':date,'factor':str(factor) if factor is not None else None,'adjusted_close':close}
  if close is None:hist=[];e12=e26=dea=k=d=None
  else:
   hist.append((close,high,low));e12=close if e12 is None else 2/13*close+11/13*e12;e26=close if e26 is None else 2/27*close+25/27*e26;dif=e12-e26;dea=dif if dea is None else .2*dif+.8*dea
   out.update(ema12=e12,ema26=e26,dif=dif,dea=dea,macd=2*(dif-dea))
  for n in (5,10,20,60):out['ma'+str(n)]=sum(x[0] for x in hist[-n:])/n if len(hist)>=n else None
  if len(hist)>=20:
   a=[x[0] for x in hist[-20:]];m=sum(a)/20;std=math.sqrt(sum((x-m)**2 for x in a)/20);out.update(boll_middle=m,boll_upper=m+2*std,boll_lower=m-2*std,boll_bandwidth=(4*std/m if m else None))
  else:out.update(boll_middle=None,boll_upper=None,boll_lower=None,boll_bandwidth=None)
  if len(hist)>=9 and all(x[1] is not None and x[2] is not None for x in hist[-9:]):
   hi=max(x[1] for x in hist[-9:]);lo=min(x[2] for x in hist[-9:]);
   if hi!=lo:
    rsv=100*(close-lo)/(hi-lo);k=(2*(50 if k is None else k)+rsv)/3;d=(2*(50 if d is None else d)+k)/3
   else:k=d=None
  else:k=d=None
  out.update(k=k,d=d,j=3*k-2*d if k is not None else None)
  for key in ('ema12','ema26','dif','dea','macd'):out.setdefault(key,None)
  expected.append(out)
  if (s,date) in [('600519','2025-06-25'),('600519','2025-06-26'),('000815','2026-11-05'),('000815','2026-11-06')]:checks.append({'symbol':s,'date':date,'raw_close':r['close'],'factor':str(factor) if factor is not None else None,'product':str(Decimal(str(r['close']))*factor) if factor is not None and r['close'] is not None else None})
jsonl('expected.jsonl',expected);dump('checks.json',checks)
summary={s:{'rows':len(by[s]),'start':str(by[s][0]['ts'])[:10] if by[s] else None,'end':str(by[s][-1]['ts'])[:10] if by[s] else None,'suspended':sum(bool(x['suspended']) for x in by[s]),'factor_source':sources[s],'event_count':len(events[s]) if events[s] is not None else None} for s in syms}
dump('capture_metadata.json',{'started_at_utc':now,'calendar_source':'/home/exedev/core-data/trade_calendar/calendar.json','queries':queries,'summary':summary,'elapsed_seconds':round(time.monotonic()-start,3)})
print(json.dumps({'summary':summary,'checks':checks,'requests':requests,'elapsed_seconds':round(time.monotonic()-start,3)},ensure_ascii=False))
