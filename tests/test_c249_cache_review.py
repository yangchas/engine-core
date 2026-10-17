from datetime import datetime
from engine_core.live_session import states_at
from engine_core import live_state_cache as c

def setup(monkeypatch,tmp_path):
 monkeypatch.setenv('CORE_DATA_ROOT',str(tmp_path));monkeypatch.delenv('CORE_SLICE_ROOT',raising=False)
 monkeypatch.setenv('CORE_STATE_CACHE','on');monkeypatch.setattr(c,'_redis_client',lambda *_:None)
 return datetime(2026,10,16,9,25,6)

def test_empty_result_must_not_prevent_retry(monkeypatch,tmp_path):
 cutoff=setup(monkeypatch,tmp_path)
 class TD:
  calls=0
  def states_at(self,cutoff):
   self.calls+=1
   return {} if self.calls==1 else {'000001':{'price_milli':10000}}
 td=TD();assert states_at(td,cutoff)=={}
 assert states_at(td,cutoff)=={'000001':{'price_milli':10000}}

def test_future_checkpoint_is_not_cached(monkeypatch,tmp_path):
 setup(monkeypatch,tmp_path);writes=[];monkeypatch.setattr(c,'write',lambda *args:writes.append(args))
 class TD:
  def states_at(self,cutoff):return {}
 states_at(TD(),datetime(2099,1,1,9,25,6));assert not writes

def test_corrupt_newest_can_use_previous(monkeypatch,tmp_path):
 cutoff=setup(monkeypatch,tmp_path);expected={f'{n:06d}':{'price_milli':10000} for n in range(1,4001)}
 c.write(cutoff,expected)
 p=c._paths(cutoff,'')[0].parent/'new.manifest.json';p.write_text('{')
 import os,time
 os.utime(p,(time.time()+10,time.time()+10))
 assert c.read(cutoff)==expected

def test_readiness_is_rechecked_even_when_states_cached(monkeypatch,tmp_path):
 setup(monkeypatch,tmp_path)
 from engine_core.live_pages import render_page
 class TD:
  ready=False
  checks=0
  def latest_tick_ts(self):
   self.checks+=1
   return datetime(2026,10,16,9,45,30) if self.ready else datetime(2026,10,16,9,45,29)
  def states_at(self,cutoff):return {'000001':{'symbol':'000001','price_milli':10000,'prev_close_milli':10000,'pct_change':0.,'match_amount_yuan':1000,'ts':cutoff.strftime('%H:%M:%S'),'source_ts':cutoff.isoformat(sep=' ')}}
 td=TD()
 _,a=render_page(td,trade_date='2026-10-16',stage='open_0946',asof='2026-10-16T09:46:00')
 td.ready=True
 _,b=render_page(td,trade_date='2026-10-16',stage='open_0946',asof='2026-10-16T09:46:00')
 assert td.checks==2
 assert a['snapshots'][-1]['status']=='NOT_READY'
 assert b['snapshots'][-1]['status']!='NOT_READY'

def test_cache_key_contains_second_precise_cutoff(monkeypatch,tmp_path):
 cutoff=setup(monkeypatch,tmp_path)
 assert c._key(cutoff)!=c._key(cutoff.replace(second=0))
 assert c._key(cutoff)!=c._key(cutoff.replace(day=15))

def test_partial_result_does_not_prevent_complete_retry(monkeypatch,tmp_path):
 cutoff=setup(monkeypatch,tmp_path)
 class TD:
  calls=0
  def states_at(self,cutoff):
   self.calls+=1
   return {'000001':{'price_milli':10000}} if self.calls==1 else {
    f'{n:06d}':{'price_milli':10000} for n in range(1,4001)}
 td=TD();assert len(states_at(td,cutoff))==1
 assert len(states_at(td,cutoff))==4000
