import json
import subprocess
import sys
from copy import deepcopy

import pytest
from engine_core.sentiment_review import (FACTOR_REGISTRY, build_timeseries_dataset,
    compute_market_global_factor, compute_ladder_relay_factor, compute_broken_rotten_factor,
    compute_top_amount_core_factor, compute_extremes_factor, compute_auction_themes_5d_factor,
    compute_kaipan_hot_factor, compute_sentiment_temperature, compute_leader_divergence_factor,
    compute_theme_competition_factor)
from engine_core.sentiment_review.adapters import load_kaipan_cache, TDReader
from engine_core.sentiment_review.adapters.kaipan_cache import available_kaipan_plates


def row(pct, amount=100_000_000, **kw):
    return {'pct': pct, 'px': 10*(1+pct/100), 'pc': 10, 'high': 11,
            'amount_yuan': amount, **kw}


def test_registry_and_market_missing():
    assert len(FACTOR_REGISTRY) == 10
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
    raw = dict(time='09:25',up_ratio=5,net_lu=5,top100_avg_pct=5,yest_2p_avg_pct=5,high_minus_low_pp=5,yw_pp=5)
    hist = [dict(raw, up_ratio=i, net_lu=i, top100_avg_pct=i, yest_2p_avg_pct=i, high_minus_low_pp=10-i, yw_pp=10-i) for i in range(5)]
    score = compute_sentiment_temperature(raw,hist,time='09:25',min_periods=3,previous_0925=50)
    assert score['temperature'] == pytest.approx(94.4444444444)
    assert score['delta_25_45'] == pytest.approx(44.4444444444) and score['regime'] == '高潮'
    assert compute_sentiment_temperature(raw,hist,time='09:35',min_periods=3)['temperature'] is None
    assert compute_sentiment_temperature({**raw,'top100_avg_pct':None},hist,min_periods=3)['temperature'] is None


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


def test_kaipan_availability_and_historical_envelope():
    records = [
        {'trade_date': '2026-11-25', 'available_at': '2026-11-26T18:59:31+08:00',
         'his_plates': {'ok': True, 'data': {'list': [['801045', '医药', 1]]}}},
        {'trade_date': '2026-11-25', 'available_at': None, 'plates': ['unknown']},
    ]
    assert available_kaipan_plates(records, '2026-11-25', '2026-11-25T15:00:00+08:00') is None
    assert available_kaipan_plates(records, '2026-11-25', '2026-11-27T09:25:00+08:00')[0]['name'] == '医药'
    captures = [{'trade_date': '2026-11-26', 'available_at': '2026-11-26T09:26:37', 'plates': ['A']}]
    assert available_kaipan_plates(captures, '2026-11-26', '2026-11-26T09:25:00') is None
    assert available_kaipan_plates(captures, '2026-11-26', '2026-11-26T09:35:00') == ['A']
    assert available_kaipan_plates(captures, '2026-11-26', '2026-11-26T09:35:00+08:00') is None


def test_cli_and_td_offline(tmp_path):
    assert 'ticks' in TDReader.TABLES
    source = tmp_path/'input.json'; dest = tmp_path/'dataset.json'
    source.write_text('{}')
    for args in [('build',str(source),str(dest)),('audit',str(dest))]:
        r = subprocess.run([sys.executable,'-m','engine_core.sentiment_review',*args],capture_output=True,text=True)
        assert r.returncode == 0, r.stderr


def test_real_loader_causality_and_retrospective(tmp_path):
    from engine_core.sentiment_review.adapters.real_loader import build_from_td_and_cache
    cache = tmp_path / 'cache'; cache.mkdir()
    live = tmp_path / 'live'; live.mkdir()
    for day in ('2026-11-24', '2026-11-25'):
        (cache / (day + '.json')).write_text(json.dumps({'trade_date': day, 'fetched_at': '2026-11-26T18:00:00+08:00', 'his_plates': {'ok': True, 'data': {'list': [['1', 'A']]}}, 'his_bans': {str(i): {'ok': True, 'data': {'info': [[['000001', 'one']] if i == 2 else []]}} for i in range(1, 6)}, 'his_zha': {'ok': True, 'data': {'list': [['000001', 'one']]}}, 'his_floor': {'ok': True, 'data': {'list': [['000001', 'one']]}}}))
    class Reader:
        def read(self, name, start, end):
            if name == 'daily_kline':
                return [{'ts': d + ' 00:00:00', 'symbol': '000001', 'close': p, 'pre_close': 10, 'high': p, 'low': 10, 'open': 10, 'amount_yuan': 1, 'turnover_pct': 10} for d,p in [('2026-11-24', 11), ('2026-11-25', 99)]]
            return []
        def read_ticks(self, day, cutoff):
            return [{'ts': day + ' 09:25:00', 'symbol': '000001', 'px_milli': 10000, 'pc_milli': 10000},
                    {'ts': day + ' 15:00:00', 'symbol': '000002', 'px_milli': 99999},
                    {'ts': day + ' 09:45:30.001', 'symbol': '000003', 'px_milli': 20000, 'pc_milli': 10000}]
        def read_members(self, asof, retrospective):
            return {'rows': [{'plate_code': '801001', 'plate_member_label': '机器人、其他', 'symbol': f'{i:06d}'} for i in range(1, 6)]}
    data = build_from_td_and_cache('', cache, live, reader=Reader())
    assert data['meta']['provenance'] == 'RETROSPECTIVE_BACKFILL'
    assert '801001' not in data['intraday'][1]['outcomes']['theme_outcomes']
    assert '机器人' in data['intraday'][1]['outcomes']['theme_outcomes']
    day = data['intraday'][1]
    assert [p['time'] for p in day['timepoints']] == ['09:25', '09:35', '09:45']
    assert day['timepoints'][0]['factors']['kaipan_hot']['prev_close'] == ['A']
    assert day['timepoints'][0]['factors']['ladder_relay']['2p']['n'] == 1
    assert day['timepoints'][0]['factors']['broken_rotten']['cohort_source'] == 'kaipan_hist_cache'
    assert day['outcomes']['close']['time'] == '15:00'
    assert day['timepoints'][2]['factors']['market_global']['eq_pct'] == 0
    assert day['timepoints'][0]['factors']['market_global']['cap_w_pct'] == 0
    assert data['daily'][1]['factors']['market_global']['cap_w_pct'] is not None
    assert data['daily'][1]['factors']['kaipan_hot']['today_close'] == ['A']
    strict = build_from_td_and_cache('', cache, live, reader=Reader(), allow_retrospective=False)
    assert strict['intraday'][1]['timepoints'][0]['factors']['kaipan_hot']['prev_close'] is None
    assert strict['intraday'][1]['timepoints'][0]['factors']['ladder_relay']['cohort_source'] == 'daily_kline_v1'
    class EmptyReader(Reader):
        def read(self, name, start, end): return []
        def read_ticks(self, day, cutoff): return []
    empty = build_from_td_and_cache('', cache, live, reader=EmptyReader())
    assert empty['intraday'][0]['timepoints'] == []
    assert empty['intraday'][0]['outcomes']['close'] is None
    assert empty['intraday'][0]['outcomes']['next_open'] is None


def test_build_real_cli_wiring(tmp_path, monkeypatch, capsys):
    from engine_core.sentiment_review import cli
    from engine_core.sentiment_review.adapters import real_loader
    calls = []
    def build(*args, **kwargs):
        calls.append((args, kwargs))
        return {'meta': {'daily_days': 0, 'intraday_tick_days': 0}, 'intraday': [], 'daily': []}
    monkeypatch.setattr(real_loader, 'build_from_td_and_cache', build)
    out = tmp_path / 'data.pkl.gz'
    assert cli.main(['build-real', '--td-config', 'cfg', '--kaipan-cache', 'cache', '--kaipan-live-root', 'live', '--out', str(out), '--strict-capture-time']) == 0
    assert calls == [(('cfg', 'cache', 'live'), {'allow_retrospective': False})]
    assert cli.main(['audit', str(out)]) == 0


def test_historical_positional_cohorts():
    from engine_core.sentiment_review.adapters.kaipan_cache import parse_historical_cohorts
    def envelope(rows): return {'ok': True, 'data': {'list': rows}}
    def ban(rows): return {'ok': True, 'data': {'info': [rows]}}
    long_row = ['000005', 'five'] + [0] * 13 + [7]
    pools = {'his_bans': {str(i): ban([['000001', 'one']] if i == 1 else [long_row] if i == 5 else []) for i in range(1, 6)},
             'his_zha': envelope([['000002', 'broken'], ['bad', 'invalid']]),
             'his_floor': envelope([['000003', 'floor']]), 'his_plates': envelope([['801001', 'plate', 9]])}
    result = parse_historical_cohorts(pools)
    assert result['official_limit_up_by_tier'][5] == {'000005'}
    assert result['official_streaks'] == {'000001': 1, '000005': 7}
    assert result['official_broken_symbols'] == {'000002'}
    assert result['official_floor_symbols'] == {'000003'}
    assert result['official_hot_plates'][0]['name'] == 'plate'
    pools['his_bans']['3'] = {'ok': False}
    assert parse_historical_cohorts(pools)['official_streaks'] is None


def test_official_cohorts_and_tick_day_history():
    cohorts = {'official_streaks': {'000002': 2}, 'official_broken_symbols': {'000003'}, 'official_floor_symbols': {'000004'}}
    days = {'2026-01-01': {'snapshots': {'15:00': {'000001': row(10)}}},
            '2026-01-02': {'snapshots': {'09:25': {'000002': row(1), '000003': row(-6), '000004': row(-7)}, '15:00': {'000001': row(0)}}, 'prior_official_cohorts': cohorts},
            '2026-01-03': {'snapshots': {'15:00': {'000001': row(0)}}}}
    data = build_timeseries_dataset(days, min_periods=1)
    assert data['meta']['daily_days'] == 3 and data['meta']['intraday_tick_days'] == 1
    assert [d['has_intraday_ticks'] for d in data['intraday']] == [False, True, False]
    day = data['intraday'][1]; factors = day['timepoints'][0]['factors']
    assert factors['ladder_relay']['cohort_source'] == 'kaipan_hist_cache'
    assert factors['ladder_relay']['2p']['n'] == 1
    assert factors['broken_rotten']['broken']['nuke_pct'] == 100
    assert factors['broken_rotten']['floor']['n'] == 1
    assert day['cohort_cross_check']['daily_kline_v1']['limit_up'] == 1
    assert day['cohort_cross_check']['kaipan_hist_cache']['limit_up'] == 1


def test_empty_intraday_snapshots_do_not_enter_temperature_history(monkeypatch):
    from engine_core.sentiment_review import pipeline
    calls = []
    original = pipeline.compute_sentiment_temperature
    def capture(current, history, **kwargs):
        if kwargs['time'] == '09:25':
            calls.append(len(history))
        return original(current, history, **kwargs)
    monkeypatch.setattr(pipeline, 'compute_sentiment_temperature', capture)
    days = {'2026-01-01': {'snapshots': {'09:25': {'000001': row(0)}}},
            '2026-01-02': {'snapshots': {'09:25': {}, '15:00': {'000001': row(0)}}},
            '2026-01-03': {'snapshots': {'09:25': {'000001': row(1)}}}}
    data = pipeline.build_timeseries_dataset(days)
    assert calls == [0, 1]
    assert [day['has_intraday_ticks'] for day in data['intraday']] == [True, False, True]


def test_fixed_fields_cutoffs_and_feedback():
    symbols = [f'{i:06d}' for i in range(5)]
    rows = {s: row(10, 1e7, float_cap=2, bid_price=11, ask_price=0, ask_volume=0) for s in symbols}
    days = {
        '2026-01-01': {'snapshots': {'15:00': {s: row(10, 1e7) for s in symbols}}},
        '2026-01-02': {'snapshots': {'09:25': rows, '09:35': rows, '09:45': rows},
                       'members': {'机器人': symbols}, 'hot_members': {'机器人概念': symbols},
                       'kaipan': {'prev_close': [{'name': '机器人'}], '09:35': [{'name': 'A'}]},
                       'prior_official_cohorts': {'official_broken_symbols': set(symbols), 'official_floor_symbols': set(symbols)}}}
    points = build_timeseries_dataset(days)['intraday'][1]['timepoints']
    early, mid, late = (p['factors'] for p in points)
    assert early['broken_rotten']['broken']['promo_pct'] == 100
    assert early['broken_rotten']['floor']['promo_pct'] == 100
    assert early['top_amount_core']['promo_pct'] == 100
    assert early['kaipan_hot']['prev_hot_member_perf']['n_symbols'] == 5
    assert mid['kaipan_hot']['today_0935'] == ['A']
    a = early['auction_themes_5d']['features'][0]
    b = late['auction_themes_5d']['features'][0]
    assert a['up25'] == 100 and a['amt35_yi'] is None and a['amt45_yi'] is None
    assert b['amt35_yi'] == .5 and b['amt45_yi'] == .5
    assert early['sentiment_score']['history_n'] == 0


def test_score_records_prior_history_and_warmup():
    raw = dict(time='09:25', up_ratio=5, net_lu=5, top100_avg_pct=5, yest_2p_avg_pct=5, high_minus_low_pp=5, yw_pp=5)
    history = [raw.copy() for _ in range(10)]
    result = compute_sentiment_temperature(raw, history, time='09:25', min_periods=11)
    assert result['history_n'] == 10 and result['temperature'] is not None
    assert compute_sentiment_temperature(raw, history[:9], time='09:25', min_periods=11)['temperature'] is None


def test_audit_reports_zero_coverage(tmp_path, capsys):
    from engine_core.sentiment_review import cli
    data = build_timeseries_dataset({'2026-01-01': {'snapshots': {'15:00': {'000001': row(0)}}}})
    path = tmp_path / 'dataset.json'
    path.write_text(json.dumps(data))
    assert cli.main(['audit', str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['coverage']['15:00']['yw_pp'] == {'present': 0, 'total': 1}
    assert any('yw_pp has zero coverage' in e for e in result['errors'])


def test_leader_structure_boundaries_and_missing():
    streaks = {'a': 3, 'b': 2, 'c': 1, 'missing': 3}
    def calc(a, b, c, sealed=()):
        return compute_leader_divergence_factor(streaks, {'a': row(a), 'b': row(b), 'c': row(c)}, set(sealed))
    x = calc(3, -2, 0)
    assert x['structure_label'] == '龙头打高度' and x['leader_symbols'] == ['a', 'missing']
    assert x['leader_count'] == 2 and x['leader_avg_pct'] == 3
    assert x['high_3p_avg_pct'] == 3 and x['mid_2b_avg_pct'] == -2 and x['mid_minus_low_pp'] == -2
    assert calc(-2, 0, .51)['structure_label'] == '高低切'
    assert calc(-2, 0, .5)['structure_label'] == '常规分化'
    assert calc(1, -1.01, 1)['structure_label'] == '淘汰中位'
    assert calc(-.1, 0, -.1)['structure_label'] == '梯队通杀'
    assert calc(0, -2, 1)['structure_label'] == '常规分化'
    empty = compute_leader_divergence_factor({}, {}, set())
    assert empty['max_h'] == 0 and empty['leader_avg_pct'] is None and empty['structure_label'] == '常规分化'


def test_theme_competition_cuts_ties_and_missing():
    features = [{'theme': '机器人概念', 'n': 5, 'p25': 1.5, 'p35': 1, 'p45': 1, 'amt25_yi': 1, 'amt35_yi': 2, 'amt45_yi': 3},
                {'theme': '银行', 'n': 5, 'p25': 1, 'p35': 2, 'p45': 2, 'amt25_yi': 3, 'amt35_yi': 4, 'amt45_yi': 5},
                {'theme': '小样本', 'n': 4, 'p25': 99, 'p35': 99, 'p45': 99, 'amt25_yi': 99}]
    a = compute_theme_competition_factor(features, ['机器人'], time='09:25')
    assert a['leader_theme'] == '机器人概念' and a['top1_vs_top2_gap_pp'] == .5
    assert a['competition_label'] == '主线胜出' and a['top3_amt_share_pct'] == 100
    b = compute_theme_competition_factor(features, ['机器人'], time='09:35', open_0925_leader_theme=a['leader_theme'])
    assert b['competition_label'] == '新题材卡位' and b['is_intraday_takeover'] is True
    missing = compute_theme_competition_factor([], None, time='09:45')
    assert missing['leader_pct'] is None and missing['is_new_theme_breakout'] is None
    assert missing['competition_label'] == '电风扇混战'


def test_daily_score_preserved_and_intraday_fallback():
    daily = dict(time='15:00', up_ratio=1, net_lu=1, seal_rate=1, yest_lu_avg_pct=1, downside_count=1)
    assert compute_sentiment_temperature(daily, time='15:00', min_periods=1)['temperature'] == 80
    d = {'2026-01-01': {'snapshots': {'15:00': {'600001': row(10)}}},
         '2026-01-02': {'snapshots': {'09:25': {'600001': row(1, float_cap=1)}}, 'prior_official_cohorts': {'official_streaks': {'600001': 1}}}}
    score = build_timeseries_dataset(d, min_periods=1)['intraday'][1]['timepoints'][0]['factors']['sentiment_score']
    assert score['pillars']['yest_2p_avg_pct'] == 100 and score['temperature'] is not None


def test_intraday_optional_divergence_and_required_pillars():
    raw = dict(time='09:45', up_ratio=2, net_lu=2, top100_avg_pct=2,
               yest_2p_avg_pct=2, high_minus_low_pp=0, yw_pp=0)
    history = [dict(raw, up_ratio=1, net_lu=1, top100_avg_pct=1,
                    yest_2p_avg_pct=1, high_minus_low_pp=1, yw_pp=1)] * 2
    score = compute_sentiment_temperature(raw, history, time='09:45', min_periods=3)
    assert score['pillars']['high_minus_low_pp'] == pytest.approx(200/3)
    assert score['pillars']['yw_pp'] == pytest.approx(200/3)
    missing = dict(raw, high_minus_low_pp=None)
    assert compute_sentiment_temperature(missing, history, time='09:45', min_periods=3)['temperature'] == pytest.approx(280/3)
    assert compute_sentiment_temperature(missing, history[:1], time='09:45', min_periods=3)['temperature'] is None
    for key in ('up_ratio', 'net_lu', 'top100_avg_pct', 'yest_2p_avg_pct', 'yw_pp'):
        assert compute_sentiment_temperature(dict(missing, **{key: None}), history, time='09:45', min_periods=3)['temperature'] is None
    daily = dict(up_ratio=1, net_lu=1, seal_rate=1, yest_lu_avg_pct=1, downside_count=1)
    for cut in (None, '15:00'):
        assert tuple(compute_sentiment_temperature(daily, time=cut, min_periods=1)['pillars']) == ('up_ratio', 'net_lu', 'seal_rate', 'yest_lu_avg_pct', 'downside_count')


def test_score_pipeline_future_invariance():
    days = {}
    for i in range(1, 5):
        rows = {'a': row(i, float_cap=1), 'b': row(-i, float_cap=3), 'c': row(i/2, float_cap=2)}
        days[f'2026-01-0{i}'] = {'snapshots': {'09:45': deepcopy(rows), '15:00': deepcopy(rows)},
                                   'prior_official_cohorts': {'official_streaks': {'a': 3, 'b': 1}}}
    before = build_timeseries_dataset(days, min_periods=2)
    changed = deepcopy(days)
    changed['2026-01-03']['snapshots']['15:00']['a']['pct'] = 99
    changed['2026-01-04']['snapshots']['09:45']['a']['pct'] = -99
    after = build_timeseries_dataset(changed, min_periods=2)
    assert before['intraday'][2]['timepoints'][0] == after['intraday'][2]['timepoints'][0]
    factors = before['intraday'][2]['timepoints'][0]['factors']
    assert factors['sentiment_score']['pillars']['high_minus_low_pp'] is not None
    assert factors['sentiment_score']['pillars']['yw_pp'] is not None
