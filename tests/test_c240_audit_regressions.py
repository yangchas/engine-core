from engine_core.daily_history import calculate_daily_indicators
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.slices import SliceReader,SliceProviders

def test_rsi_uses_first_period_seed_then_each_remaining_delta_once():
 closes=[10,11,10,11,10,11,10,12]
 rows=[{'symbol':'000001','date':f'2026-10-{i+1:02d}','close':v} for i,v in enumerate(closes)]
 result=calculate_daily_indicators('000001',rows)
 assert result['rsi_6']==64.29

def test_missing_latest_indicator_is_not_replaced_by_prior_date(tmp_path):
 c=ArtifactCatalog(tmp_path)
 for day,close in [('2026-10-14',10),('2026-10-15',20)]:
  c.commit('daily_kline',day,[{'symbol':'000001','date':day,'close':close}],{'available_at':day+'T17:30:00+08:00'},source='baostock')
 c.commit('daily_indicators','2026-10-14',[{'symbol':'000001','date':'2026-10-14','ma5':99}],{'available_at':'2026-10-14T17:30:00+08:00'},source='core_derived')
 r=SliceReader(SliceProviders(catalog=c,redis=None,file_fallback=False).providers()).get('daily_history',trade_date='2026-10-16',asof='2026-10-16T09:26:00+08:00',prior_trade_date='2026-10-15')
 latest=[x for x in r.rows if x['date']=='2026-10-15'][0]
 assert latest['ma5'] is None
 assert r.meta['quality']['indicator_coverage']==0

def test_reindex_appends_corrected_indicators_without_network(tmp_path):
 from engine_core.daily_history import reindex_daily_indicators
 c=ArtifactCatalog(tmp_path)
 for i,v in enumerate([10,11,10,11,10,11,10,12]):
  d=f'2026-10-{i+1:02d}'
  c.commit('daily_kline',d,[{'symbol':'000001','date':d,'close':v,'open':v,'high':v,'low':v,'volume':1,'amount':1}],{'available_at':d+'T17:30:00+08:00'},source='baostock')
 r=reindex_daily_indicators(c,start='2026-10-01',end='2026-10-08')
 assert r['days_reindexed']==8

def test_reindex_counts_kline_that_landed_after_1730(tmp_path):
 from engine_core.daily_history import reindex_daily_indicators
 c=ArtifactCatalog(tmp_path)
 for i,v in enumerate([10,11,10,11,10,11,10,12]):
  d=f'2026-10-{i+1:02d}'
  at=d+('T18:15:04+08:00' if i==7 else 'T17:30:00+08:00')
  c.commit('daily_kline',d,[{'symbol':'000001','date':d,'close':v,'open':v,'high':v,'low':v,'volume':1,'amount':1}],{'available_at':at},source='baostock')
 reindex_daily_indicators(c,start='2026-10-08',end='2026-10-08')
 rows=c.read_rows(c.locate('daily_indicators','2026-10-08'))
 assert len(rows)==1 and rows[0]['rsi_6'] is not None
 meta=__import__('json').loads(c.locate('daily_indicators','2026-10-08').read_text())
 assert meta['available_at'].startswith('2026-10-08T18:15:04')
