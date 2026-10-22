from datetime import datetime,timedelta
from engine_core import live_state_cache as c
from engine_core import live_session as session

def env(monkeypatch,tmp_path):
 monkeypatch.setenv('CORE_DATA_ROOT',str(tmp_path));monkeypatch.delenv('CORE_SLICE_ROOT',raising=False)
 monkeypatch.setenv('CORE_STATE_CACHE','on');monkeypatch.setattr(c,'_redis_client',lambda *_:None)
 return datetime(2026,10,16,9,25,6)

def test_sql_connection_checks_window_ready_before_caching(monkeypatch,tmp_path):
 cutoff=env(monkeypatch,tmp_path);writes=[]
 class SQLConnection:
  def cursor(self):raise AssertionError('query replaced below')
 states={f'{i:06d}':{'price_milli':10000} for i in range(4000)}
 monkeypatch.setattr(session,'_sql_states',lambda td,cutoff:states)
 monkeypatch.setattr(session,'_latest_tick',lambda td,cutoff:cutoff-timedelta(seconds=1))
 monkeypatch.setattr(c,'write',lambda *args:writes.append(args))
 assert len(session.states_at(SQLConnection(),cutoff))==4000
 assert writes==[], 'SQL connection without latest_tick_ts must still check TD readiness'

def test_persisted_empty_snapshot_is_ignored(monkeypatch,tmp_path):
 cutoff=env(monkeypatch,tmp_path);c.write(cutoff,{})
 class TD:
  calls=0
  def states_at(self,cutoff):self.calls+=1;return {'000001':{'price_milli':10000}}
 td=TD();assert session.states_at(td,cutoff)=={'000001':{'price_milli':10000}}
 assert td.calls==1

def test_persisted_partial_snapshot_is_ignored(monkeypatch,tmp_path):
 cutoff=env(monkeypatch,tmp_path);c.write(cutoff,{'000001':{'price_milli':10000}})
 class TD:
  def states_at(self,cutoff):return {f'{i:06d}':{'price_milli':10000} for i in range(4000)}
 assert len(session.states_at(TD(),cutoff))==4000

def test_oversize_report_is_written_with_warning(monkeypatch,tmp_path):
 from engine_core import daily_pipeline as dp
 from engine_core.slices import SliceReader,SliceProviders
 from engine_core.data_pipeline.storage import ArtifactCatalog
 monkeypatch.setenv('CORE_STATE_CACHE','off');monkeypatch.setenv('CORE_DATA_ROOT',str(tmp_path/'data'))
 monkeypatch.setattr(dp,'_shrink_live_report',lambda report,**kw:report.update(audit_padding='x'*260000))
 class TD:
  def states_at(self,cutoff):return {}
  def latest_tick_ts(self):return None
 class Redis:
  def get(self,key):return None
  def hgetall(self,key):return {}
  def scan_iter(self,**kwargs):return iter([])
 slices=SliceReader(SliceProviders(catalog=ArtifactCatalog(tmp_path/'data'),redis=None,file_fallback=False).providers())
 result=dp.run_daily_pipeline(stage='auction',trade_date='2026-10-16',out_root=tmp_path/'out',dry_run=True,sources=dp.DailySources(td=TD(),redis=Redis(),slice_reader=slices))
 import json
 from pathlib import Path
 out=Path(result['out_dir']);manifest=json.loads((out/'manifest.json').read_text())
 assert (out/'report.json').stat().st_size>250000
 assert manifest['report_json_bytes']==(out/'report.json').stat().st_size
 assert any('250 KB' in w and 'audit_padding' in w for w in manifest['warnings'])

def test_auction_points_slice_respects_availability(monkeypatch,tmp_path):
 from engine_core.data_pipeline.storage import ArtifactCatalog
 from engine_core.slices import SliceReader,SliceProviders
 from conftest import use_declared_time_as_test_commit_clock
 cat=use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path))
 cat.commit('auction_points','2026-10-16',[{'symbol':'000001','point_ts':1792113906000,'price_milli':None}],{'trade_date':'2026-10-16','available_at':'2026-10-16T09:25:06+08:00'},source='t1_v2_redis_archive')
 reader=SliceReader(SliceProviders(catalog=cat,redis=None,file_fallback=False).providers())
 early=reader.get('auction_points',trade_date='2026-10-16',asof='2026-10-16T09:24:00+08:00')
 late=reader.get('auction_points',trade_date='2026-10-16',asof='2026-10-16T09:26:00+08:00')
 assert early.rows==[]
 assert late.meta['source']=='core_data' and late.rows[0]['price_milli'] is None

def test_legacy_hot_plate_fallback_is_explicit(monkeypatch,tmp_path):
 from engine_core.slices import SliceProviders,SliceReader
 from engine_core.data_pipeline.storage import ArtifactCatalog
 import json
 class Redis:
  def get(self,key):return None
  def hgetall(self,key):return {'chip':json.dumps({'name':'芯片','strength':10})}
 result=SliceReader(SliceProviders(catalog=ArtifactCatalog(tmp_path),redis=Redis(),file_fallback=False).providers()).get('hot_plates_today',trade_date='2026-10-16',asof='2026-10-16T09:26:00+08:00',prior_trade_date='2026-10-15')
 assert result.meta['source']=='legacy_redis'
 assert result.meta['status']=='DEGRADED' and result.meta['legacy_dependency'] is True
