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
