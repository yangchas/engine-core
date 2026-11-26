"""Research command-line interface."""
import argparse
from .adapters import load_dataset, history_records, snapshots_from_dataset
from .engine import RegimeHistoryStore, StrategyEngine, default_strategy_specs
from .ledger import SingleAccountLedger
from engine_core.sentiment_review.adapters.td_reader import TDReader


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
    back.add_argument('--cost-bps', default='20,50')
    shadow = sub.add_parser('shadow-run')
    shadow.add_argument('--trade-date', required=True)
    shadow.add_argument('--td-config', required=True)
    shadow.add_argument('--shadow-dir', required=True)
    args = parser.parse_args(argv)
    if args.command == 'seed-history':
        store = RegimeHistoryStore(args.out)
        records = history_records(load_dataset(args.dataset))
        for record in records:
            store.append(record)
        print(f'Seeded {len(records)} history records')
        return
    if args.command == 'shadow-run':
        parser.error('shadow-run requires a point-in-time source for candidate names, theme membership and prior landmine facts; no adapter is configured')
    dataset = load_dataset(args.dataset)
    snapshots = list(snapshots_from_dataset(dataset, TDReader(config=args.td_config)))
    if not snapshots:
        parser.error('dataset contains no strategy snapshots')
    print('strategy                 cost_bps    final_nav    max_drawdown    sell_lockups')
    for cost in (float(x) for x in args.cost_bps.split(',')):
        for spec in default_strategy_specs():
            ledger = SingleAccountLedger(cost_bps=cost)
            engine = StrategyEngine(spec, ledger)
            history = []
            for snapshot in snapshots:
                decision = spec.regime.detect(snapshot, history)
                selection = spec.selector.select(snapshot)
                intents = spec.policy.plan(snapshot, decision, selection, ledger.positions)
                ledger.execute_intraday(snapshot.trade_date, intents, snapshot.quotes_0945)
                if snapshot.eod_close is None:
                    raise ValueError('backtest snapshot missing EOD closes')
                ledger.mark_eod(snapshot.trade_date, snapshot.eod_close)
                history.append({**snapshot.regime_raw, 'trade_date': snapshot.trade_date, 'time': '09:45'})
            print(f'{spec.name:24} {cost:8.0f} {ledger.nav[-1]["nav"]:12.6f} {ledger.max_drawdown:15.2%} {ledger.sell_lockups:15d}')
