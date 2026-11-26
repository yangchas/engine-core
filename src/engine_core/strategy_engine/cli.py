"""Read-only research and local shadow command-line interface."""
import argparse
import json
from pathlib import Path
from .adapters import load_dataset, extract_regime_history, load_backtest_snapshots, load_live_shadow_snapshot
from .engine import RegimeHistoryStore, StrategyEngine, default_strategy_specs
from .ledger import SingleAccountLedger


def _simulate(snapshots, spec, cost, omit=None):
    ledger = SingleAccountLedger(cost_bps=cost)
    history, batches, batch_dates = [], {}, {}
    signals = buys = 0
    invested = []
    for index, snapshot in enumerate(snapshots):
        settlement = index == len(snapshots)-1
        regime = spec.regime.detect(snapshot, history)
        selection = spec.selector.select(snapshot)
        # All strategies use the same ten-session warmup and settlement is exit-only.
        active = index >= 10 and not settlement
        if active and regime.target_exposure > 0:
            signals += 1
        intents = spec.policy.plan(snapshot, regime, selection, ledger.positions)
        if not active or snapshot.trade_date == omit:
            intents = tuple(i for i in intents if i.side == 'SELL')
        result = ledger.execute_intraday(snapshot.trade_date, intents, snapshot.quotes_0945)
        bought = False
        for trade in result['trades']:
            batch = trade['batch_id']
            if trade['side'] == 'BUY':
                bought = True
                batch_dates[batch] = snapshot.trade_date
                batches[batch] = -trade['qty']*trade['price']-trade['fee']
            else:
                batches[batch] += trade['qty']*trade['price']-trade['fee']
        buys += int(bought)
        if snapshot.eod_close is None:
            raise ValueError('backtest snapshot missing EOD closes')
        for pos in ledger.positions:
            close = snapshot.eod_close.get(pos.symbol)
            if not isinstance(close, (int, float)) or close <= 0:
                raise ValueError(f'missing EOD close {snapshot.trade_date} {pos.symbol}')
        mark = ledger.mark_eod(snapshot.trade_date, snapshot.eod_close)
        if active or settlement:
            invested.append((mark['equity']-mark['cash'])/mark['equity'])
        if not settlement:
            history.append({**snapshot.regime_raw, 'trade_date': snapshot.trade_date, 'time': '09:45'})
    for pos in ledger.positions:
        batches[pos.batch_id] += pos.qty * snapshots[-1].eod_close[pos.symbol]
    by_day = {}
    for batch, pnl in batches.items():
        day = batch_dates[batch]
        by_day[day] = by_day.get(day, 0) + pnl
    best = max(by_day, key=by_day.get) if by_day else None
    return {'return': (ledger.nav[-1]['nav']-1)*100, 'mdd': ledger.max_drawdown*100,
            'buys': buys, 'signals': signals, 'invested': 100*sum(invested)/len(invested) if invested else 0,
            'locks': ledger.sell_lockups, 'best': best}


def _write_json(path, value):
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)+'\n', encoding='utf-8')
    temp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m engine_core.strategy_engine')
    sub = parser.add_subparsers(dest='command', required=True)
    seed = sub.add_parser('seed-history')
    seed.add_argument('--dataset', required=True)
    seed.add_argument('--out', required=True)
    back = sub.add_parser('backtest')
    back.add_argument('--dataset', required=True)
    back.add_argument('--td-config', required=True)
    back.add_argument('--kaipan-cache', required=True)
    back.add_argument('--stock-static')
    back.add_argument('--cost-bps', default='20,50')
    shadow = sub.add_parser('shadow-run')
    shadow.add_argument('--trade-date', required=True)
    shadow.add_argument('--td-config', required=True)
    shadow.add_argument('--kaipan-cache')
    shadow.add_argument('--stock-static')
    shadow.add_argument('--shadow-dir', required=True)
    args = parser.parse_args(argv)
    if args.command == 'seed-history':
        records = extract_regime_history(load_dataset(args.dataset))
        # Replace the complete file only after validating all records.
        text = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in records)
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix+'.tmp')
        temp.write_text(text, encoding='utf-8')
        temp.replace(path)
        print(f'Seeded {len(records)} history records')
        return
    if args.command == 'shadow-run':
        root = Path(args.shadow_dir)
        root.mkdir(parents=True, exist_ok=True)
        store = RegimeHistoryStore(root/'regime_history.jsonl')
        snapshot = load_live_shadow_snapshot(args.trade_date, args.td_config, args.kaipan_cache, args.stock_static)
        for spec in default_strategy_specs():
            state = root/(spec.name+'.json')
            log = root/(spec.name+'.jsonl')
            saved = json.loads(state.read_text(encoding='utf-8')) if state.exists() else None
            if saved and saved['last_trade_date'] >= args.trade_date:
                if saved['last_trade_date'] == args.trade_date:
                    continue
                raise ValueError('shadow runs must be chronological')
            ledger = SingleAccountLedger.from_dict(saved['ledger']) if saved else SingleAccountLedger(cost_bps=spec.cost_bps)
            decision = StrategyEngine(spec, ledger, store).run_intraday(snapshot)
            decision['source_metadata'] = snapshot.source_metadata
            with log.open('a', encoding='utf-8') as out:
                out.write(json.dumps(decision, ensure_ascii=False, allow_nan=False)+'\n')
            _write_json(state, {'last_trade_date': args.trade_date, 'ledger': ledger.to_dict()})
            print(json.dumps(decision, ensure_ascii=False, allow_nan=False))
        return
    snapshots = load_backtest_snapshots(args.dataset, args.td_config, args.kaipan_cache, args.stock_static)
    print('RETROSPECTIVE_BACKFILL: latest plate membership, static names and historical Kaipan cohorts; historical visibility is unverified.')
    print(f'Signal dates: {snapshots[10].trade_date} .. {snapshots[-2].trade_date}; settlement: {snapshots[-1].trade_date}')
    print('| Strategy | Cost bps | Net Return % | EOD MDD % | Return/MDD | Buys/Signals | Avg Invested % | Sell Lockups | Ex-Best-Batch Return % |')
    print('|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for cost in (float(x) for x in args.cost_bps.split(',')):
        for spec in default_strategy_specs():
            r = _simulate(snapshots, spec, cost)
            without = _simulate(snapshots, spec, cost, r['best'])['return'] if r['best'] else r['return']
            ratio = f"{r['return']/r['mdd']:.2f}" if r['mdd'] else 'NA'
            print(f"| {spec.name} | {cost:g} | {r['return']:+.2f} | {r['mdd']:.2f} | {ratio} | {r['buys']}/{r['signals']} | {r['invested']:.2f} | {r['locks']} | {without:+.2f} |", flush=True)
