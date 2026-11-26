"""Read-only research and local shadow command-line interface."""
import argparse
import contextlib
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .adapters import load_dataset, extract_regime_history, load_backtest_snapshots, load_live_shadow_snapshot
from .engine import RegimeHistoryStore, StrategyEngine, default_strategy_specs
from .ledger import SingleAccountLedger
from .mailer import build_intraday_shadow_email, build_eod_shadow_email, send_shadow_email
from dataclasses import asdict


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


def _send_from_logs(root, args, *, intraday):
    records = {}
    for spec in default_strategy_specs():
        path = root / (spec.name + ('.jsonl' if intraday else '.eod.jsonl'))
        matches = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                   if line.strip() and json.loads(line).get('trade_date') == args.trade_date]
        if len(matches) != 1:
            raise ValueError(f'missing or duplicate shadow record: {spec.name} {args.trade_date}')
        records[spec.name] = matches[0]
    builder = build_intraday_shadow_email if intraday else build_eod_shadow_email
    options = {'recipients': [x.strip() for x in args.email_to.split(',')]}
    if args.subject_prefix is not None:
        options['subject_prefix'] = args.subject_prefix
    receipt = send_shadow_email(builder(args.trade_date, records, **options))
    return asdict(receipt)


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
    eod = sub.add_parser('shadow-eod')
    eod.add_argument('--trade-date', required=True)
    eod.add_argument('--td-config', required=True)
    eod.add_argument('--shadow-dir', required=True)
    guardian = sub.add_parser('guardian-run')
    guardian.add_argument('--mode', required=True, choices=('intraday', 'eod'))
    guardian.add_argument('--td-config', required=True)
    guardian.add_argument('--shadow-dir', required=True)
    guardian.add_argument('--stock-static')
    for command_parser in (shadow, eod, guardian):
        command_parser.add_argument('--email-to')
        command_parser.add_argument('--subject-prefix')
    args = parser.parse_args(argv)
    if args.command == 'guardian-run':
        try:
            request = json.load(sys.stdin)
            trade_date = request.get('trade_date') or request.get('date') or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            date.fromisoformat(trade_date)
            command = ['shadow-run' if args.mode == 'intraday' else 'shadow-eod',
                       '--trade-date', trade_date, '--td-config', args.td_config, '--shadow-dir', args.shadow_dir]
            if args.mode == 'intraday' and args.stock_static:
                command += ['--stock-static', args.stock_static]
            if args.email_to:
                command += ['--email-to', args.email_to]
            if args.subject_prefix is not None:
                command += ['--subject-prefix', args.subject_prefix]
            with contextlib.redirect_stdout(sys.stderr):
                receipt = main(command)
            root = Path(args.shadow_dir)
            suffix = '.jsonl' if args.mode == 'intraday' else '.eod.jsonl'
            artifacts = [str(root / (spec.name + suffix)) for spec in default_strategy_specs()
                         if (root / (spec.name + suffix)).exists()]
            response = {'contract': 1, 'status': 'ok', 'artifacts': artifacts,
                        'detail': f'{args.mode} completed for {trade_date}',
                        'email_status': receipt['status'] if receipt else 'skipped'}
            if receipt:
                response['email_receipt'] = receipt
            code = 0
        except Exception as exc:
            response = {'contract': 1, 'status': 'failed', 'error_code': 'SHADOW_ERROR',
                        'retryable': True, 'artifacts': [], 'detail': str(exc),
                        'email_status': 'error' if args.email_to else 'skipped'}
            code = 1
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return code
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
            decision['regime_raw'] = snapshot.regime_raw
            decision['positions'] = ledger.to_dict()['positions']
            with log.open('a', encoding='utf-8') as out:
                out.write(json.dumps(decision, ensure_ascii=False, allow_nan=False)+'\n')
            _write_json(state, {**(saved or {}), 'last_trade_date': args.trade_date, 'ledger': ledger.to_dict()})
            print(json.dumps(decision, ensure_ascii=False, allow_nan=False))
        if args.email_to:
            return _send_from_logs(root, args, intraday=True)
        return
    if args.command == 'shadow-eod':
        from engine_core.sentiment_review.adapters.td_reader import TDReader
        date.fromisoformat(args.trade_date)
        root = Path(args.shadow_dir)
        rows = TDReader(config=args.td_config).read('daily_kline', args.trade_date, args.trade_date)
        prices = {}
        for row in rows:
            symbol = str(row.get('symbol') or '')
            if symbol in prices:
                raise ValueError(f'duplicate daily bar: {args.trade_date} {symbol}')
            if symbol:
                prices[symbol] = row.get('close')
        for spec in default_strategy_specs():
            state = root / (spec.name + '.json')
            if not state.exists():
                continue
            saved = json.loads(state.read_text(encoding='utf-8'))
            if saved.get('last_eod_date') == args.trade_date:
                continue
            if saved.get('last_eod_date', '') > args.trade_date or saved['last_trade_date'] > args.trade_date:
                raise ValueError('shadow marks must be chronological')
            ledger = SingleAccountLedger.from_dict(saved['ledger'])
            for pos in ledger.positions:
                price = prices.get(pos.symbol)
                if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                    raise ValueError(f'missing EOD close {args.trade_date} {pos.symbol}')
            mark = ledger.mark_eod(args.trade_date, prices)
            mark['strategy'] = spec.name
            mark['positions'] = ledger.to_dict()['positions']
            with (root / (spec.name + '.eod.jsonl')).open('a', encoding='utf-8') as out:
                out.write(json.dumps(mark, ensure_ascii=False, allow_nan=False) + '\n')
            _write_json(state, {**saved, 'last_eod_date': args.trade_date, 'ledger': ledger.to_dict()})
            print(json.dumps(mark, ensure_ascii=False, allow_nan=False))
        if args.email_to:
            return _send_from_logs(root, args, intraday=False)
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
