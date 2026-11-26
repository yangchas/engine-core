import json
from dataclasses import replace

import pytest

from engine_core.strategy_engine import (MarketSnapshot, StockCandidate, SelectionResult, RegimeDecision,
    Position, OrderIntent, WeatherSoloRegime, Pos20Regime, MainThemeNormalizedAccelSelector,
    SyncSwitchPortfolioPolicy, SingleAccountLedger, RegimeHistoryStore, StrategyEngine, default_strategy_specs)


def snapshot(**kwargs):
    values = dict(trade_date='2026-01-12', prior_trade_date='2026-01-09', asof_ts='2026-01-12T09:45:30',
                  regime_raw={'gap': 1, 'top100_avg_pct': 2, 'pos20_med': 35, 'up_ratio': 1, 'net_lu': 1,
                              'yest_2p_avg_pct': 1, 'high_minus_low_pp': 1, 'yw_pp': 1},
                  themes=[{'theme': 'A', 'p45': 3}], candidates=[], quotes_0945={})
    values.update(kwargs)
    return MarketSnapshot(**values)


def candidate(symbol, **kwargs):
    values = dict(symbol=symbol, name='公司', theme='A', ret_25=0, ret_45=6, intraday_pos_45=.8,
                  amt_45_yuan=4e7, ask1=10, av1=100, prev_streak=0, prev_broken=0)
    values.update(kwargs)
    return StockCandidate(**values)


def test_snapshot_guards():
    with pytest.raises(ValueError): snapshot(prior_trade_date='2026-01-12')
    with pytest.raises(ValueError): snapshot(asof_ts='2026-01-11T09:45:30')


def test_regime_swaps_and_warmup():
    snap = snapshot()
    assert WeatherSoloRegime().detect(snap).label == 'RED'
    history = [{**snap.regime_raw, 'trade_date': f'2025-12-{i:02}', 'time': '09:45'} for i in range(1, 11)]
    assert WeatherSoloRegime().detect(snap, history).label == 'WARM_SOLO'
    assert Pos20Regime().detect(snap).label == 'WARM'
    assert Pos20Regime(40).detect(snap).label == 'COLD'
    assert WeatherSoloRegime().detect(replace(snap, regime_raw={**snap.regime_raw, 'gap': .1}), history).label == 'CROWDED'
    assert WeatherSoloRegime(temp_threshold=101).detect(snap, history).label == 'COLD_SOLO'


def test_selection_funnel_and_gem_cap():
    cs = [candidate('300001', ret_45=12), candidate('301002', ret_45=11), candidate('300003', ret_45=10),
          candidate('600001', ret_45=5), candidate('000001', prev_streak=2), candidate('000002', prev_broken=1),
          candidate('000003', intraday_pos_45=.69), candidate('000004', amt_45_yuan=1),
          candidate('000005', ret_45=8.01), candidate('000006', ask1=0), candidate('688001'),
          candidate('600002', name='ST stock')]
    result = MainThemeNormalizedAccelSelector().select(snapshot(candidates=cs))
    assert result.funnel_counts == {'theme_pool': 12, 'after_universe': 10, 'after_landmine': 8, 'after_buyable': 7, 'after_guardrails': 4, 'selected': 3, 'gem_count': 2}
    assert [p[0] for p in result.picks] == ['300001', '301002', '600001']
    assert result.theme_details == ({'theme': 'A', 'p45': 3},)
    assert result.evidence[0] == {'rank': 1, 'symbol': '300001', 'name': '公司', 'board': '创业板', 'limit_pct': 20.0, 'theme': 'A', 'ret_25': 0, 'ret_45': 12, 'path_25_45': 12, 'norm_accel': .6, 'intraday_pos_45': .8, 'amt_45_yi': .4, 'ask1': 10, 'av1': 100, 'prev_streak': 0, 'prev_broken': 0}


@pytest.mark.parametrize('label,exposure,cap,expected', [('WARM_SOLO', 1, .2, .4), ('COLD_SOLO', .5, .1, .2)])
def test_weight_cap_and_cash(label, exposure, cap, expected):
    picks = SelectionResult(('A',), (('600001', 1, 10, 'A'), ('600002', 1, 10, 'A')), {})
    regime = RegimeDecision(label, exposure, cap, {}, ())
    quotes = {s: {'ap1_milli': 10000, 'av1': 100} for s, *_ in picks.picks}
    intents = SyncSwitchPortfolioPolicy().plan(snapshot(quotes_0945=quotes), regime, picks, [])
    ledger = SingleAccountLedger(cost_bps=0)
    ledger.execute_intraday('2026-01-12', intents, quotes)
    assert ledger.cash == pytest.approx(1e6 * (1 - expected))


def test_hold_policy_and_execution():
    pos = Position('600001', 100, 10, '2026-01-09', '1')
    quotes = {'600001': {'ap1_milli': 0, 'av1': 0, 'bp1_milli': 11000, 'bv1': 100}}
    snap = snapshot(quotes_0945=quotes)
    regime = RegimeDecision('COLD', 0, 0, {}, ())
    selection = SelectionResult((), (), {})
    assert len(SyncSwitchPortfolioPolicy().plan(snap, regime, selection, [pos])) == 1
    assert not SyncSwitchPortfolioPolicy(True).plan(snap, regime, selection, [pos])
    ledger = SingleAccountLedger(initial_cash=1000, cost_bps=20)
    ledger.positions = [pos, Position('600002', 10, 10, '2026-01-12', '2')]
    result = ledger.execute_intraday('2026-01-12', [OrderIntent('SELL', '600001'), OrderIntent('SELL', '600002')], {'600001': {'bp1_milli': 0, 'bv1': 0}})
    assert result['sell_lockups'] == 1 and result['rejected'] == 1 and len(ledger.positions) == 2
    result = ledger.execute_intraday('2026-01-13', [OrderIntent('SELL', '600001'), OrderIntent('BUY', '600003', 2)], {'600001': {'bp1_milli': 11000, 'bv1': 1}, '600003': {'ap1_milli': 10000, 'av1': 1}})
    assert result['trades'][0]['fee'] == pytest.approx(1.1)
    assert result['trades'][1]['fee'] > 0 and ledger.cash >= 0
    ledger.mark_eod('2026-01-13', {'600002': 9, '600003': 10})
    assert SingleAccountLedger.from_dict(json.loads(json.dumps(ledger.to_dict()))).to_dict() == ledger.to_dict()


def test_history_and_engine(tmp_path):
    store = RegimeHistoryStore(tmp_path / 'history.jsonl')
    for day in ('2026-01-11', '2026-01-09', '2026-01-10'):
        store.append({'trade_date': day, 'time': '09:45'})
    store.append({'trade_date': '2026-01-10', 'time': '09:45'})
    assert [r['trade_date'] for r in store.before('2026-01-11')] == ['2026-01-09', '2026-01-10']
    with pytest.raises(ValueError): store.append({'trade_date': '2026-01-10', 'time': '15:00'})
    engine = StrategyEngine(default_strategy_specs()[2], SingleAccountLedger(), store)
    assert engine.run_intraday(snapshot())['regime']['label'] == 'WARM'
    assert engine.run_eod('2026-01-12', {})['nav'] == 1


def test_extract_regime_history_and_seed(tmp_path, monkeypatch, capsys):
    from engine_core.strategy_engine import cli
    from engine_core.strategy_engine.adapters import extract_regime_history
    factors = {'market_global': {'up_ratio': 0, 'yw_pp': 2, 'pos20_med': 3},
               'extremes': {'sealed_up': 2, 'sealed_down': 3},
               'ladder_relay': {'2p': {'avg_pct': None}, '1b': {'avg_pct': 0}},
               'top_amount_core': {'avg_pct': 4}, 'leader_divergence': {'high_minus_low_pp': 5},
               'theme_competition': {'top1_vs_top2_gap_pp': 6}}
    def day(d):
        return {'trade_date': d, 'timepoints': [{'trade_date': d, 'time': '09:45', 'factors': factors}]}
    data = {'intraday': [day('2026-01-02'), day('2026-01-01')]}
    records = extract_regime_history(data)
    assert [r['trade_date'] for r in records] == ['2026-01-01', '2026-01-02']
    assert records[0]['net_lu'] == -1 and records[0]['yest_2p_avg_pct'] == 0
    monkeypatch.setattr(cli, 'load_dataset', lambda _: data)
    out = tmp_path/'regime_history.jsonl'
    cli.main(['seed-history', '--dataset', 'unused', '--out', str(out)])
    first = out.read_text()
    cli.main(['seed-history', '--dataset', 'unused', '--out', str(out)])
    assert out.read_text() == first
    assert [json.loads(line) for line in first.splitlines()] == records


def test_shadow_run_persists_and_is_idempotent(tmp_path, monkeypatch):
    from engine_core.strategy_engine import cli
    snap = snapshot(candidates=[candidate('600001')], quotes_0945={'600001': {'ap1_milli': 10000, 'av1': 100, 'px_milli': 10000}})
    monkeypatch.setattr(cli, 'load_live_shadow_snapshot', lambda *args: snap)
    args = ['shadow-run', '--trade-date', snap.trade_date, '--td-config', 'unused', '--shadow-dir', str(tmp_path)]
    cli.main(args)
    cli.main(args)
    for spec in default_strategy_specs():
        log = [json.loads(line) for line in (tmp_path/(spec.name+'.jsonl')).read_text().splitlines()]
        assert len(log) == 1
        assert log[0]['selection']['funnel_counts']['selected'] == 1
        assert log[0]['stock_names']['600001'] == '公司'
        state = json.loads((tmp_path/(spec.name+'.json')).read_text())
        assert state['last_trade_date'] == snap.trade_date
    assert len((tmp_path/'regime_history.jsonl').read_text().splitlines()) == 1


def test_shadow_adapter_uses_prior_bars_and_membership_fallback(monkeypatch):
    from engine_core.strategy_engine import adapters
    from engine_core.sentiment_review.adapters import td_reader
    calls = []
    class Reader:
        def __init__(self, **kwargs):
            assert kwargs == {'config': 'cfg'}
        def read(self, table, start, end):
            calls.append((table, start, end))
            return [{'ts': '2026-01-09', 'symbol': '600001', 'close': 10, 'high': 11, 'low': 9,
                     'amount_yuan': 1000, 'turnover_pct': 1}]
        def read_members(self, asof, retrospective):
            calls.append(('members', asof, retrospective))
            return {'rows': [{'symbol': '600001', 'plate_name': 'A', 'stock_name': '公司'}]} if retrospective else None
        def read_ticks(self, day, cutoff):
            calls.append(('ticks', day, cutoff))
            return [{'symbol': '600001', 'ts': day+' '+cutoff, 'px_milli': 10000, 'pc_milli': 10000,
                     'h_milli': 11000, 'l_milli': 9000, 'ap1_milli': 10000, 'av1': 1}]
    monkeypatch.setattr(td_reader, 'TDReader', Reader)
    monkeypatch.setattr(adapters, '_read_names', lambda path: {})
    snap = adapters.load_live_shadow_snapshot('2026-01-12', 'cfg')
    assert ('members', '2026-01-12T09:45:30+08:00', False) in calls
    assert ('members', None, True) in calls
    assert ('daily_kline', '2025-10-14', '2026-01-11') in calls
    assert snap.source_metadata['membership'] == 'latest_fallback'
    assert snap.source_metadata['kaipan'] == 'daily_kline_v1_fallback'
    assert not snap.candidates
    assert snap.regime_raw['yest_2p_avg_pct'] is None


def test_kline_fallback_streak_and_broken():
    from engine_core.strategy_engine.adapters import _kline_cohorts, _candidates
    dates = ['2026-01-07', '2026-01-08', '2026-01-09']
    bars = {
        dates[0]: {'300001': {'pre_close': 10, 'close': 10, 'high': 10}},
        dates[1]: {'300001': {'pre_close': 10, 'close': 12, 'high': 12}},
        dates[2]: {
            '300001': {'pre_close': 10, 'close': 12, 'high': 12},
            '600001': {'pre_close': 10, 'close': 10.9, 'high': 11},
            '000001': {'pre_close': 10.05, 'close': 11.06, 'high': 11.06},
            '600002': {'pre_close': 10, 'close': 10, 'high': 10},
            '600003': {'close': 10, 'high': 11},
        },
    }
    cohorts = _kline_cohorts(bars, dates)
    assert cohorts['official_streaks']['300001'] == 2
    assert cohorts['official_streaks']['600001'] == 0
    assert cohorts['official_streaks']['600002'] == 0
    assert cohorts['official_broken_symbols'] == {'600001'}
    # Half-up rounding makes 10.05 * 1.10 = 11.06; earlier missing bars
    # leave the length of this streak unknown.
    rows = _candidates({'A': set(bars[dates[-1]])}, {}, {}, {}, cohorts, [{'theme': 'A', 'p45': 1}])
    by_symbol = {row['symbol']: row for row in rows}
    assert by_symbol['000001']['prev_streak'] is None
    assert by_symbol['000001']['prev_broken'] == 0
    assert by_symbol['600003']['prev_streak'] is None
    assert by_symbol['600003']['prev_broken'] is None


def test_candidate_missing_quote_and_unavailable_cohort_are_not_zero():
    from engine_core.strategy_engine.adapters import _candidates
    ticks25 = {'600001': {'px_milli': 0, 'pc_milli': 10000}}
    ticks45 = {'600001': {'px_milli': 10500, 'pc_milli': 10000, 'h_milli': 11000,
                          'l_milli': 10000, 'ap1_milli': 10500, 'av1': 10}}
    rows = _candidates({'A': {'600001'}}, {'600001': '公司'}, ticks25, ticks45, {},
                       [{'theme': 'A', 'p45': 2}])
    assert rows[0]['ret_25'] is None
    assert rows[0]['prev_streak'] is None and rows[0]['prev_broken'] is None


def test_shadow_eod_marks_once_and_requires_held_closes(tmp_path, monkeypatch):
    from engine_core.strategy_engine import cli
    from engine_core.sentiment_review.adapters import td_reader
    calls = []
    class Reader:
        def __init__(self, config):
            assert config == 'cfg'
        def read(self, table, start, end):
            calls.append((table, start, end))
            return [{'symbol': '600001', 'close': 12}]
    monkeypatch.setattr(td_reader, 'TDReader', Reader)
    spec = default_strategy_specs()[0]
    ledger = SingleAccountLedger()
    ledger.positions = [Position('600001', 10, 10, '2026-01-09', '1')]
    state = tmp_path / (spec.name + '.json')
    state.write_text(json.dumps({'last_trade_date': '2026-01-12', 'ledger': ledger.to_dict()}))
    args = ['shadow-eod', '--trade-date', '2026-01-12', '--td-config', 'cfg', '--shadow-dir', str(tmp_path)]
    cli.main(args)
    cli.main(args)
    saved = json.loads(state.read_text())
    assert saved['last_eod_date'] == '2026-01-12'
    assert len(saved['ledger']['nav']) == 1
    assert json.loads((tmp_path / (spec.name + '.eod.jsonl')).read_text())['equity'] == 1000120
    assert calls == [('daily_kline', '2026-01-12', '2026-01-12')] * 2
    monkeypatch.setattr(Reader, 'read', lambda *args: [])
    with pytest.raises(ValueError, match='missing EOD close'):
        cli.main(['shadow-eod', '--trade-date', '2026-01-13', '--td-config', 'cfg', '--shadow-dir', str(tmp_path)])
    assert json.loads(state.read_text()) == saved


@pytest.mark.parametrize('mode', ['intraday', 'eod'])
def test_guardian_contract(tmp_path, monkeypatch, capsys, mode):
    import io
    import sys
    from engine_core.strategy_engine import cli
    from engine_core.sentiment_review.adapters import td_reader
    class Reader:
        def __init__(self, config): pass
        def read(self, *args): return []
    monkeypatch.setattr(td_reader, 'TDReader', Reader)
    monkeypatch.setattr(cli, 'load_live_shadow_snapshot', lambda *args: snapshot())
    args = ['guardian-run', '--mode', mode, '--td-config', 'cfg', '--shadow-dir', str(tmp_path)]
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"date":"2026-01-12"}'))
    assert cli.main(args) == 0
    result = capsys.readouterr()
    assert len(result.out.splitlines()) == 1
    response = json.loads(result.out)
    assert response['contract'] == 1 and response['status'] == 'ok'
    monkeypatch.setattr(sys, 'stdin', io.StringIO('invalid'))
    assert cli.main(args) == 1
    result = capsys.readouterr()
    assert len(result.out.splitlines()) == 1
    response = json.loads(result.out)
    assert response['status'] == 'failed' and response['error_code'] == 'SHADOW_ERROR'
    assert response['retryable'] is True and response['artifacts'] == []


def test_shadow_email_rendering():
    from engine_core.strategy_engine.mailer import build_intraday_shadow_email, build_eod_shadow_email
    names = [s.name for s in default_strategy_specs()]
    decision = {'regime': {'label': 'WARM_SOLO', 'target_exposure': 1, 'metrics': {'temperature': 53, 'gap': 2, 'top100_avg_pct': 3}},
                'regime_raw': {'pos20_med': 40}, 'selection': {'themes': ['芯片'], 'funnel_counts': {'theme_pool': 10, 'after_universe': 9, 'after_landmine': 8, 'after_buyable': 7, 'after_guardrails': 6, 'selected': 1, 'gem_count': 0}},
                'execution': {'cash': 900, 'trades': [{'side': 'BUY', 'symbol': '600001', 'qty': 10, 'price': 10, 'fee': 1}]},
                'positions': [{'symbol': '600001', 'qty': 10, 'cost_basis': 10}], 'source_metadata': {'membership': 'as_of', 'kaipan': 'fallback', 'stock_names': 'static'}}
    msg = build_intraday_shadow_email('2026-01-12', {n: decision for n in names}, recipients=['412826313@qq.com'])
    assert str(msg['Subject']) == '[影子策略 09:46] 2026-01-12 WARM_SOLO | 买1卖0'
    body = msg.get_body(preferencelist=('plain',)).get_content()
    assert all(x in body for x in ('temp45=53', 'pos20=40', '芯片', 'after_guardrails=6', '600001', '手续费=1.00', 'membership=as_of', *names))
    assert msg.get_body(preferencelist=('html',)) is not None
    selection = MainThemeNormalizedAccelSelector().select(snapshot(candidates=[candidate('600001', name='测试公司')], themes=[{'theme': 'A', 'p45': 3.5}]))
    from dataclasses import asdict
    decision['selection'] = asdict(selection)
    decision['stock_names'] = {'600001': '测试公司'}
    decision['regime'].update(label='CROWDED', target_exposure=0, max_single_weight=0)
    rendered = build_intraday_shadow_email('2026-01-12', {n: decision for n in names}, recipients=['412826313@qq.com']).get_body(preferencelist=('html',)).get_content()
    assert '<pre>' not in rendered
    assert all(x in rendered for x in ('今日候补观察（未买入）', '测试公司', '+3.50%', '09:25→09:45', '归一化分', '09:45成交额', '昨日基因', '板块池', '精选 Top5', '创业板 0只'))
    mark = {'nav': 1.02, 'max_drawdown': .03, 'cash': 900, 'equity': 1020, 'positions': decision['positions']}
    msg = build_eod_shadow_email('2026-01-12', {n: mark for n in names}, recipients=['412826313@qq.com'])
    assert str(msg['Subject']) == '[影子策略 15:25盯市] 2026-01-12 | main_a_p2 NAV=1.0200 (+2.00%)'
    body = msg.get_body(preferencelist=('plain',)).get_content()
    assert all(x in body for x in ('MDD%=3.00', '持仓市值=120.00', '600001', *names))
    mark['stock_names'] = {'600001': '测试公司'}
    rendered = build_eod_shadow_email('2026-01-12', {n: mark for n in names}, recipients=['412826313@qq.com']).get_body(preferencelist=('html',)).get_content()
    assert '<pre>' not in rendered and all(x in rendered for x in ('<table', '最大回撤 MDD%', '持仓市值', '测试公司'))
    with pytest.raises(ValueError, match='recipient'):
        build_eod_shadow_email('2026-01-12', {n: mark for n in names}, recipients=['other@example.com'])


@pytest.mark.parametrize('mode', ['intraday', 'eod'])
def test_guardian_mail_failure_retry(tmp_path, monkeypatch, capsys, mode):
    import io
    import sys
    from engine_core.strategy_engine import cli
    from engine_core.report_mailer import SendReceipt
    from engine_core.sentiment_review.adapters import td_reader
    class Reader:
        def __init__(self, config): pass
        def read(self, *args): return []
    monkeypatch.setattr(td_reader, 'TDReader', Reader)
    monkeypatch.setattr(cli, 'load_live_shadow_snapshot', lambda *args: snapshot())
    if mode == 'eod':
        cli.main(['shadow-run', '--trade-date', '2026-01-12', '--td-config', 'cfg', '--shadow-dir', str(tmp_path)])
        capsys.readouterr()
    args = ['guardian-run', '--mode', mode, '--td-config', 'cfg', '--shadow-dir', str(tmp_path), '--email-to', '412826313@qq.com', '--subject-prefix', '[preview]']
    def fail(msg):
        raise RuntimeError('SMTP failed')
    monkeypatch.setattr(cli, 'send_shadow_email', fail)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"trade_date":"2026-01-12"}'))
    assert cli.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed' and result['retryable'] and result['email_status'] == 'error'
    def send(msg):
        assert str(msg['To']) == '412826313@qq.com'
        assert str(msg['Subject']).startswith('[preview]')
        return SendReceipt('sent', 1, 123, 0.1, None)
    monkeypatch.setattr(cli, 'send_shadow_email', send)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"trade_date":"2026-01-12"}'))
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['email_status'] == 'sent' and result['email_receipt']['recipient_count'] == 1


def test_shadow_sender_uses_local_state_and_enforces_recipient(tmp_path, monkeypatch):
    from email.message import EmailMessage
    from engine_core.strategy_engine import mailer
    from engine_core.report_mailer import SendReceipt
    from types import SimpleNamespace
    monkeypatch.setenv('CORE_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(mailer.report_mailer, 'load_smtp_config', lambda path: SimpleNamespace(from_addr='sender@example.com'))
    def send(cfg, msg, *, dry_run, state):
        assert not dry_run and state.claim('key', 'sending', 60)
        with pytest.raises(RuntimeError, match='in progress'):
            state.claim('key', 'sending', 60)
        state.set('key', 'sent', 60)
        assert not state.claim('key', 'sending', 60)
        assert state.get('key') == 'sent'
        return SendReceipt('sent', 1, 1, 0, None)
    monkeypatch.setattr(mailer.report_mailer, 'send_message', send)
    msg = EmailMessage()
    msg['To'] = '412826313@qq.com'
    assert mailer.send_shadow_email(msg).status == 'sent'
    msg['Cc'] = 'other@example.com'
    with pytest.raises(ValueError, match='recipient'):
        mailer.send_shadow_email(msg)
