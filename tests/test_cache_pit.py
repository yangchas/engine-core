from engine_core.prep_trade_day import _prepare_theme_snapshot_cache
from engine_core.data_pipeline.storage import ArtifactCatalog
from pathlib import Path
import json
from engine_core.slices import SliceProviders,SliceReader
from engine_core.limit_reason_theme import read_theme_snapshot_cache,theme_snapshot_version,theme_snapshot_visibility,merge_theme_snapshot_static_fallback,load_kaipan_theme_snapshot
from conftest import use_declared_time_as_test_commit_clock

class Redis:
 def __init__(self):self.values={}
 def set(self,key,value,ex):assert key.startswith('core:');self.values[key]=value
 def get(self,key):return self.values.get(key)

def scenario(tmp_path,*,future_name=False,late_pool=False):
 c=use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path));day='2026-10-15';report_day='2026-10-16';prep=report_day+'T08:50:00+08:00';asof=report_day+'T09:26:00+08:00'
 fixture=Path(__file__).parent/'fixtures'/'trade_calendar';source=json.loads((fixture/'baostock-2026-01-01_2026-12-31.json').read_bytes())
 dates=[r['calendar_date'] for r in source['rows'] if r['is_trading_day']=='1'];raw={p.name:p.read_bytes() for p in fixture.glob('*.json')}
 from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
 calendar=TradeCalendarStore(tmp_path);calendar.publish_confirmed(dates,source='C325 BaoStock+SZSE',fetched_at='2026-10-20T00:00:00+00:00',coverage_start='2026-01-01',coverage_end='2026-12-31',raw_responses=raw)
 window_dates=calendar.trading_dates('2026-10-01','2026-10-16')
 def commit(ds,rows):return c.commit(ds,day,rows,{'trade_date':day,'available_at':report_day+'T08:00:00+08:00'},source='fixture')
 pool={'symbol':'000001','available_at':report_day+('T09:10:00+08:00' if late_pool else 'T08:00:00+08:00')}
 if late_pool:pool.update(plate='池题材',plate_code='801002')
 commit('limit_up_pool',[pool]);commit('hot_plates',[{'plate_code':'801001','name':'热板'}])
 commit('plate_registry',[{'code':'801001','name':'未来名称' if future_name else '原因题材','available_at':report_day+('T10:00:00+08:00' if future_name else 'T08:00:00+08:00')}])
 commit('limit_reasons',[{'symbol':'000001','Date':day,'Type':'0','ZSCode':['801001'],'Reason':'原因'}])
 commit('daily_kline',[{'symbol':'000001','date':day,'close':10}])
 r=Redis();assert _prepare_theme_snapshot_cache(c,report_day,day,prep,r)
 rd=SliceReader(SliceProviders(catalog=c,redis=None,file_fallback=False).providers())
 pool=rd.get('yest_limit_pool',trade_date=report_day,asof=asof,prior_trade_date=day,source_date=day)
 registry=rd.get('plate_registry',trade_date=report_day,asof=asof)
 reasons=rd.get('limit_reason_history',trade_date=report_day,asof=asof)
 hot=rd.get('hot_plates_yesterday',trade_date=report_day,asof=asof,prior_trade_date=day,source_date=day)
 inputs={'trade_date':report_day,'previous_trade_date':day,'pool_version':pool.meta['version'],'hot_version':hot.meta['version'],'registry_version':registry.meta['version'],'reason_versions':{str(x['trade_date']):str(x.get('version') or 'unknown') for x in reasons.meta['lineage'] if x.get('trade_date')},'calendar_dates':window_dates, 'visibility':theme_snapshot_visibility({'pool':pool.rows,'registry':registry.rows})}
 cached=read_theme_snapshot_cache(r,report_day,day,asof=asof,version=theme_snapshot_version(inputs))
 names={str(row.get('plate_code') or row.get('code')):str(row['name']) for row in registry.rows if row.get('name')}
 direct=load_kaipan_theme_snapshot(None,report_date=report_day,previous_trade_date=day,observed_at=asof,plate_names=names,static_themes_by_symbol={},pool_rows=pool.rows,reason_rows=reasons.rows,hot_rows=hot.rows,trading_dates=window_dates)
 merged=(merge_theme_snapshot_static_fallback(cached,{},expected_previous_date=day,plate_names=names,trading_dates=window_dates)
         if cached is not None else direct)
 return merged,direct,cached,str(r.values[f'core:cache:data:theme_snapshot:{report_day}'])

def test_future_registry_name_never_survives_cached_path(tmp_path):
 cached,direct,snapshot,raw=scenario(tmp_path,future_name=True)
 assert direct['assignments']['000001']['theme_name']=='801001'
 assert '未来名称' not in str(direct)
 assert '未来名称' not in raw
 assert snapshot is None or snapshot['assignments']==direct['assignments']
 assert cached['assignments']==direct['assignments']

def test_same_version_newly_visible_pool_invalidates_snapshot(tmp_path):
 cached,direct,snapshot,_raw=scenario(tmp_path,late_pool=True)
 assert direct['assignments']['000001']['theme_source']=='POOL_LABEL'
 assert snapshot is None
 assert cached['assignments']==direct['assignments']
