import json
import subprocess
import sys
from copy import deepcopy

import pytest
from engine_core.sentiment_review import (FACTOR_REGISTRY, build_timeseries_dataset,
    compute_market_global_factor, compute_ladder_relay_factor, compute_broken_rotten_factor,
    compute_top_amount_core_factor, compute_extremes_factor, compute_auction_themes_5d_factor,
    compute_kaipan_hot_factor, compute_sentiment_temperature)
from engine_core.sentiment_review.adapters import load_kaipan_cache, TDReader


def row(pct, amount=100_000_000, **kw):
    return {'pct': pct, 'px': 10*(1+pct/100), 'pc': 10, 'high': 11,
            'amount_yuan': amount, **kw}


def test_registry_and_market_missing():
    assert len(FACTOR_REGISTRY) == 8
    r = compute_market_global_factor({'a': row(10, float_cap=3, pos20=80, ma20=10, chip_profit_ratio=.6),
                                      'b': row(-10, float_cap=1)}, [1,2,3])
    assert r['eq_pct'] == 0 and r['cap_w_pct'] == 5 and r['yw_pp'] == -5
    assert r['up_ratio'] == 50 and r['amt_vs_5d'] == 1
    assert r['above_ma20_pct'] == 100 and r['chip_prof_med'] == 60
    assert compute_market_global_factor({})['tot_amt_yi'] is None
    assert compute_market_global_factor({'x': {'pct': None}})['up_ratio'] is None


def test_ladder_feedback_and_top_amount():
    rows = {'a': row(10), 'b': row(-5), 'c': row(1)}
    ladder = compute_ladder_relay_factor(rows, {'a': 1, 'b': 2, 'c': 3, 'missing': 3}, {'a','c'})
    assert ladder['1b']['promo_pct'] == 100
    assert ladder['2p']['avg_pct'] == -2 and ladder['2p']['nuke_pct'] == 50
    assert ladder['max_h']['n'] == 1
    groups = compute_broken_rotten_factor(rows, {'b'}, {'missing'})
    assert groups['broken']['nuke_pct'] == 100 and groups['rotten']['avg_pct'] is None
    prior = {str(i): {'amount_yuan': i} for i in range(101)}
    assert compute_top_amount_core_factor({'0': row(20), '100': row(-5)}, prior)['nuke_pct'] == 100


def test_extremes_daily_and_orderbook():
    rows = {'600001': row(10, open=10, low=10), '600002': row(0, open=10, low=9),
            '600003': row(-10, high=10, open=10, low=9),
            '300001': row(20, high=12, open=12, low=12)}
    x = compute_extremes_factor(rows)
    assert (x['sealed_up'],x['broken_up'],x['sealed_down'],x['nuke_5pct']) == (2,1,1,1)
    assert x['sets']['rotten'] == {'600001'}
    assert x['seal_rate'] == pytest.approx(200/3)
    tick = row(10, bid_price=11, bid_volume=1000, ask_price=0, ask_volume=0)
    assert compute_extremes_factor({'600001':tick})['seal_amt_yi'] == pytest.approx(11000/1e8)
    assert compute_extremes_factor({})['seal_rate'] is None


def test_theme_cuts_and_separate_outcomes():
    syms = [f'{i:06}' for i in range(5)]
    snaps = {'09:25': {s: row(1,10_000_000) for s in syms},
             '09:35': {s: row(2,20_000_000) for s in syms},
             '09:45': {s: row(3,40_000_000) for s in syms}}
    features = {s: {'pos20': 50, 'above_ma20': True, 'dde5_yuan': 1e8} for s in syms}
    r = compute_auction_themes_5d_factor({'T':syms},snaps,{'T':[25e6]*3},features,{'T':{'outcome_close_pct': 9}})
    x = r['features'][0]
    assert x['crowd5'] == 2 and x['relay'] == 2 and x['p45'] == 3
    assert x['ma20_pct'] == 100 and x['dde5_yi'] == 5
    assert 'outcome_close_pct' not in x and r['outcomes']['T']['outcome_close_pct'] == 9
    early = compute_auction_themes_5d_factor({'T':syms},{'09:25':snaps['09:25']})['features'][0]
    assert early['p35'] is None and early['p45'] is None


def test_kaipan_and_temperature():
    assert compute_kaipan_hot_factor(['A','B'],['B','C'])['prev_to_open_overlap'] == 1
    raw = dict(time='09:25',up_ratio=5,net_lu=5,seal_rate=5,yest_lu_avg_pct=5,downside_count=5)
    hist = [dict(raw, up_ratio=i, net_lu=i, seal_rate=i, yest_lu_avg_pct=i, downside_count=10-i) for i in range(5)]
    score = compute_sentiment_temperature(raw,hist,time='09:25',min_periods=3,previous_0925=50)
    assert score['temperature'] == pytest.approx(96.6666666667)
    assert score['delta_25_45'] == pytest.approx(46.6666666667) and score['regime'] == '高潮'
    assert compute_sentiment_temperature(raw,hist,time='09:35',min_periods=3)['temperature'] is None
    assert compute_sentiment_temperature({**raw,'seal_rate':None},hist,min_periods=3)['temperature'] is None


def test_pipeline_no_future_leak_and_baselines():
    syms = [f'{i:06}' for i in range(5)]
    snapshots = {'09:25': {s:row(1,1e7) for s in syms}, '09:35':{s:row(2,2e7) for s in syms},
                 '09:45':{s:row(3,4e7) for s in syms}, '15:00':{s:row(10,5e7,open=10,low=10) for s in syms}}
    days = {'2026-01-01':{'snapshots':snapshots,'members':{'T':syms}},
            '2026-01-02':{'snapshots':deepcopy(snapshots),'members':{'T':syms}}}
    original = deepcopy(days)
    result = build_timeseries_dataset(days,min_periods=1)
    assert days == original
    early = result['intraday'][0]['timepoints'][0]['factors']['auction_themes_5d']['features'][0]
    assert early['p45'] is None
    changed = deepcopy(days); changed['2026-01-01']['snapshots']['09:45'][syms[0]]['pct'] = 99
    assert build_timeseries_dataset(changed,min_periods=1)['intraday'][0]['timepoints'][0] == result['intraday'][0]['timepoints'][0]
    assert result['intraday'][1]['timepoints'][0]['factors']['ladder_relay']['1b']['n'] == 5


def test_cache_formats(tmp_path):
    (tmp_path/'2026-01-01.json').write_text(json.dumps({'trade_date':'2026-01-01','fetched_at':'2026-02-01','his_plates':[{'name':'A'}],'his_bans':[], 'his_zha':[], 'his_floor':[]}))
    (tmp_path/'hot_092630.json').write_text(json.dumps({'captured_local':'2026-01-02T09:26:38','response':{'list':[['801','bank',42]]}}))
    data = load_kaipan_cache(tmp_path)
    assert data[0]['available_at'] == '2026-02-01'
    assert data[1]['available_at'].endswith('09:26:38') and data[1]['plates'][0]['name'] == 'bank'
    assert data[1]['plates'][0]['raw_fields'] == [42]


def test_cli_and_td_offline(tmp_path):
    assert 'ticks' in TDReader.TABLES
    source = tmp_path/'input.json'; dest = tmp_path/'dataset.json'
    source.write_text('{}')
    for args in [('build',str(source),str(dest)),('audit',str(dest))]:
        r = subprocess.run([sys.executable,'-m','engine_core.sentiment_review',*args],capture_output=True,text=True)
        assert r.returncode == 0, r.stderr
