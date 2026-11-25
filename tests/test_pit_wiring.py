"""PIT close entry wiring; real artifact samples live in fixtures/c345/core_samples.json."""
import datetime
import pytest
from engine_core import daily_pipeline as dp


def test_supported_posthoc_env_reaches_close_pipeline(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(dp, "run_daily_pipeline", lambda **kwargs: (captured.update(kwargs) or {
        "out_dir": tmp_path, "manifest": {"degraded": [], "report_json_bytes": 1}}))
    monkeypatch.setenv("POSTHOC_DEV", "1")
    assert dp.main(["--stage", "close", "--trade-date", "2026-10-16", "--dry-run"]) == 0
    assert captured["close_basis"] == "POSTHOC_REVIEW"


def test_posthoc_entrypoint_keeps_live_default(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.delenv("POSTHOC_DEV", raising=False)
    monkeypatch.setattr(dp, "run_daily_pipeline", lambda **kwargs: (captured.update(kwargs) or {
        "out_dir": tmp_path, "manifest": {"degraded": [], "report_json_bytes": 1}}))
    assert dp.main(["--stage", "close", "--trade-date", "2026-10-16", "--dry-run"]) == 0
    assert captured["close_basis"] == "LIVE_PIT"


def test_posthoc_rejects_same_day_request(monkeypatch, tmp_path):
    class SameDay(datetime.date):
        @classmethod
        def today(cls): return cls(2026, 10, 16)
    monkeypatch.setattr(dp, "date", SameDay)
    with pytest.raises(ValueError, match="strictly before"):
        dp.run_daily_pipeline(stage="close", trade_date="2026-10-16", dry_run=True,
                              close_basis="POSTHOC_REVIEW", out_root=tmp_path)
"""C345 review: real immutable Core rows, omission represents source failure only."""
import os,json,math
from pathlib import Path
from datetime import datetime as DateTime
import pytest
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.daily_pipeline import load_close_catalog_inputs
from engine_core.close_review import derive_close_review
C=ArtifactCatalog('/home/exedev/core-data')

@pytest.fixture(scope='module')
def real_data():return load_close_catalog_inputs(C,'2026-10-16','2026-10-15',asof='2026-10-16T17:40:00+08:00',basis='POSTHOC_REVIEW')

def test_live_rejects_late_real_auction_inputs(monkeypatch,real_data):
 monkeypatch.delenv('POSTHOC_DEV',raising=False)
 rows,m=load_close_catalog_inputs(C,'2026-10-16','2026-10-15',asof='2026-10-16T17:40:00+08:00',basis='LIVE_PIT')
 assert rows and m['datasets']['daily_kline']['status']=='READY'
 assert DateTime.fromisoformat(m['datasets']['auction_panorama']['available_at'])>DateTime.fromisoformat('2026-10-16T17:40:00+08:00')
 assert m['datasets']['auction_panorama']['status']!='READY'
 assert not any(r.get('auction_pct') is not None for r in rows)

def test_posthoc_late_inputs_are_explicitly_labeled(monkeypatch):
 monkeypatch.setenv('POSTHOC_DEV','1')
 rows,m=load_close_catalog_inputs(C,'2026-10-16','2026-10-15',asof='2026-10-16T17:40:00+08:00',basis='POSTHOC_REVIEW')
 assert rows and m.get('basis')=='POSTHOC_REVIEW'

class Omit:
 def __init__(self,base,missing):self.base,self.missing=base,missing
 def locate(self,ds,day):return None if ds in self.missing else self.base.locate(ds,day)
 def read_rows(self,p):return self.base.read_rows(p)

def test_missing_yesterday_pool_does_not_become_false():
 rows,m=load_close_catalog_inputs(Omit(C,{'limit_up_pool'}),'2026-10-19','2026-10-16',asof='2026-10-19T17:40:00+08:00',basis='POSTHOC_REVIEW')
 assert m['datasets']['limit_up_pool']['status']=='MISSING'
 assert rows and all(r['in_yest_pool'] is None and r['yesterday_limit_up'] is None for r in rows)

def test_pool_coverage_counts_known_false_and_true(real_data):
 rows,m=real_data
 assert m['datasets']['limit_up_pool']['status']=='READY'
 expected=sum(r['in_yest_pool'] is not None for r in rows)
 assert m['field_coverage']['yesterday_limit_up']['valid_n']==expected

def test_missing_checkpoints_preserve_market_statistics(real_data):
 rows,m=real_data
 d=derive_close_review(rows,trade_date='2026-10-16',market={'amount_yuan':sum(r['amount_yuan'] for r in rows if r.get('amount_yuan') is not None)},checkpoint_status={k:'MISSING' for k in ['0932','0935','0940','0945']})
 assert d['market']['n']>5000 and d['market']['amount_yuan']>0
 assert d['checkpoint_chapters']['timeline']['status']=='DEGRADED'
 assert d['checkpoint_chapters']['visibility']['status']=='DEGRADED'

def test_posthoc_human_renderers_disclose_basis_and_late_sources(real_data):
 from engine_core.close_review import render_close_markdown, render_close_html
 rows,m=real_data
 report=derive_close_review(rows,trade_date='2026-10-16',
     market={'amount_yuan':sum(r['amount_yuan'] for r in rows if r.get('amount_yuan') is not None)},
     checkpoint_status={k:'MISSING' for k in ['0932','0935','0940','0945']})
 report['data_quality']['basis']=m['basis']
 report['data_quality']['late_sources']=m['late_sources']
 markdown=render_close_markdown(report)
 html=render_close_html(report,markdown)
 assert '历史回放口径' in markdown and 'POSTHOC_REVIEW' in markdown
 assert m['late_sources'][0] in markdown and '历史回放口径' in html

def test_three_real_stock_prices_and_null_auction():
 rows,m=load_close_catalog_inputs(C,'2026-10-16','2026-10-15',asof='2026-10-16T17:40:00+08:00',basis='POSTHOC_REVIEW');by={r['symbol']:r for r in rows}
 daily={r['symbol']:r for r in C.read_rows(C.locate('daily_kline','2026-10-16'))}
 auc={r['symbol']:r for r in C.read_rows(C.locate('auction_stock_facts','2026-10-16'))}
 for symbol in ['000001','000002','600519']:
  assert by[symbol]['close']==daily[symbol]['close']
  # The retained 2026-10-16 auction artifact was not available until 2026-10-19;
  # historical close reads must keep it out of a 2026-10-16 17:40 as-of.
  assert by[symbol]['auction_price'] is None
 assert m['datasets']['auction_stock_facts']['status']=='UNAVAILABLE'
 assert 'auction_stock_facts' in m['late_sources']
 rows,m=load_close_catalog_inputs(C,'2026-10-19','2026-10-16',asof='2026-10-19T17:40:00+08:00',basis='POSTHOC_REVIEW');by={r['symbol']:r for r in rows}
 assert by['000002']['auction_price'] is None


def test_live_1500_rejects_late_daily():
 rows,m=load_close_catalog_inputs(C,'2026-10-16','2026-10-15',asof='2026-10-16T15:00:00+08:00',basis='LIVE_PIT')
 assert not rows and m['datasets']['daily_kline']['status']=='UNAVAILABLE'
 assert m['basis']=='LIVE_PIT'

def test_pool_tristate_coverage_sums(real_data):
 rows,m=real_data;c=m['field_coverage']['yesterday_limit_up']
 assert c['valid_n']==c['positive_n']+c['negative_n']==len(rows)
 assert c['unknown_n']==0 and c['positive_n']>0 and c['negative_n']>0


def test_loader_rejects_posthoc_for_current_day():
    from datetime import date
    import pytest
    from engine_core.daily_pipeline import load_close_catalog_inputs
    with pytest.raises(ValueError, match="strictly before"):
        load_close_catalog_inputs(None, date.today().isoformat(), None, basis="POSTHOC_REVIEW")
