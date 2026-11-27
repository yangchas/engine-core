"""Strategy-independent, single-account simulated execution."""
from dataclasses import asdict
from math import isfinite
from .contracts import Position


def _price(quote, side):
    value = quote.get(side + '1_milli')
    if value is not None:
        return value / 1000
    return quote.get(side + '1')


def _positive(value):
    return isinstance(value, (int, float)) and isfinite(value) and value > 0


class SingleAccountLedger:
    def __init__(self, initial_cash=1_000_000.0, cost_bps=20.0):
        if not _positive(initial_cash) or not 0 <= cost_bps < 20000:
            raise ValueError('invalid cash or transaction cost')
        self.initial_cash, self.cash, self.cost_bps = float(initial_cash), float(initial_cash), float(cost_bps)
        self.positions = []
        self.nav = []
        self.sell_lockups = 0
        self.max_drawdown = 0.0
        self._batch = 0

    def execute_intraday(self, trade_date, intents, quotes_0945, *, buy_quotes=None, sell_quotes=None, buy_before_sell=False):
        buy_quotes = buy_quotes or quotes_0945
        sell_quotes = sell_quotes or quotes_0945
        intents = tuple(intents)
        trades, lockups, rejected = [], 0, 0
        fee = self.cost_bps / 20000

        def sell():
            nonlocal lockups, rejected
            for intent in (i for i in intents if i.side == 'SELL'):
                for pos in list(self.positions):
                    if pos.symbol != intent.symbol:
                        continue
                    if pos.buy_date >= trade_date:
                        rejected += 1
                        continue
                    q = sell_quotes.get(pos.symbol, {})
                    px = _price(q, 'bp')
                    if not _positive(px) or not _positive(q.get('bv1')):
                        lockups += 1
                        continue
                    gross = pos.qty * px
                    self.cash += gross * (1 - fee)
                    self.positions.remove(pos)
                    trades.append({'side': 'SELL', 'symbol': pos.symbol, 'qty': pos.qty, 'price': px, 'fee': gross * fee, 'batch_id': pos.batch_id})

        def buy():
            nonlocal rejected
            def mark(pos):
                q = buy_quotes.get(pos.symbol, {})
                px = q.get('px_milli')
                px = px / 1000 if px is not None else q.get('px')
                return pos.qty * (px if _positive(px) else pos.cost_basis)
            equity = self.cash + sum(mark(p) for p in self.positions)
            for intent in (i for i in intents if i.side == 'BUY'):
                q = buy_quotes.get(intent.symbol, {})
                px = _price(q, 'ap')
                if not _positive(px) or not _positive(q.get('av1')) or not _positive(intent.target_weight):
                    rejected += 1
                    continue
                spend = min(self.cash, intent.target_weight * equity)
                if spend <= 0:
                    continue
                qty = spend / (px * (1 + fee))
                gross = qty * px
                self.cash = max(0.0, self.cash - gross * (1 + fee))
                self._batch += 1
                batch = str(self._batch)
                self.positions.append(Position(intent.symbol, qty, px, trade_date, batch))
                trades.append({'side': 'BUY', 'symbol': intent.symbol, 'qty': qty, 'price': px, 'fee': gross * fee, 'batch_id': batch})
            return equity

        if buy_before_sell:
            equity = buy()
            sell()
        else:
            sell()
            equity = buy()
        self.sell_lockups += lockups
        return {'trades': trades, 'sell_lockups': lockups, 'rejected': rejected, 'equity_before_buys': equity, 'cash': self.cash}

    def mark_eod(self, trade_date, close_prices):
        value = 0.0
        for pos in self.positions:
            px = close_prices.get(pos.symbol)
            if isinstance(px, dict):
                px = px.get('close')
            value += pos.qty * (px if _positive(px) else pos.cost_basis)
        equity = self.cash + value
        peak = max([self.initial_cash, equity] + [r['equity'] for r in self.nav])
        self.max_drawdown = max(self.max_drawdown, 1 - equity / peak)
        record = {'trade_date': trade_date, 'cash': self.cash, 'equity': equity, 'nav': equity / self.initial_cash, 'max_drawdown': self.max_drawdown}
        self.nav.append(record)
        return record

    def to_dict(self):
        return {'initial_cash': self.initial_cash, 'cash': self.cash, 'cost_bps': self.cost_bps, 'positions': [asdict(p) for p in self.positions], 'nav': self.nav, 'sell_lockups': self.sell_lockups, 'max_drawdown': self.max_drawdown, 'batch': self._batch}

    @classmethod
    def from_dict(cls, data):
        ledger = cls(data['initial_cash'], data['cost_bps'])
        ledger.cash = data['cash']
        ledger.positions = [Position(**p) for p in data['positions']]
        ledger.nav = list(data['nav'])
        ledger.sell_lockups = data['sell_lockups']
        ledger.max_drawdown = data['max_drawdown']
        ledger._batch = data['batch']
        return ledger
