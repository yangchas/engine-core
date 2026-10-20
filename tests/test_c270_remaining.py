import json
from pathlib import Path
from engine_core import daily_pipeline as dp
from engine_core.slices import SliceResult

def test_pipeline_supplies_prior_trade_date_to_d06(tmp_path):
 class Stop(Exception):pass
 captured={}
 class Reader:
  def get(self,name,**kw):
   if name=='stock_theme_assignment':
    captured.update(kw);raise Stop()
   rows=[{'date':'2026-10-15'}] if name=='trading_calendar' else []
   return SliceResult.create(rows,{'slice_id':name,'status':'READY'})
 import pytest
 with pytest.raises(Stop):
  dp.run_daily_pipeline(stage='auction',trade_date='2026-10-16',asof='09:26:00',out_root=tmp_path/'unused',dry_run=True,sources=dp.DailySources(redis=object(),slice_reader=Reader()))
 assert captured.get('prior_trade_date')=='2026-10-15',captured

def test_real_mail_does_not_claim_missing_daily_kline():
 import pytest
 from engine_core.report_mailer import build_report_message
 p=Path('/home/exedev/validation/c304-diagnose/2026-10-16/auction_0926')
 if not (p/'manifest.json').is_file():
  pytest.skip('真实 C304 2026-10-16 auction fixture 缺失；无法验证邮件正文契约')
 m=json.loads((p/'manifest.json').read_text())
 assert m['slices']['daily_history']['status']=='READY'
 assert m['slices']['daily_report_features']['status']=='READY'
 msg=build_report_message('Core','2026-10-16','auction',p/'report.html',p/'report.json',['412826313@qq.com'],'audit@localhost',manifest=m)
 body=msg.get_body(preferencelist=('plain',)).get_content()
 assert '日K：MISSING' not in body
