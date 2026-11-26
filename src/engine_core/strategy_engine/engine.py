"""Composition and dated rolling history."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from .regime import WeatherSoloRegime, Pos20Regime
from .selector import MainThemeNormalizedAccelSelector
from .portfolio import SyncSwitchPortfolioPolicy


class RegimeHistoryStore:
    def __init__(self, path):
        self.path = Path(path)

    def before(self, current_date):
        if not self.path.exists():
            return []
        records = {}
        with self.path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    item = json.loads(line)
                    if item['trade_date'] < current_date:
                        records[item['trade_date']] = item
        return [records[d] for d in sorted(records)]

    def append(self, record):
        date = record['trade_date']
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    previous = json.loads(line)
                    if previous['trade_date'] == date:
                        if previous != record:
                            raise ValueError('conflicting history for trade date')
                        return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')


@dataclass(frozen=True)
class StrategySpec:
    name: str
    regime: object
    selector: object
    policy: object
    cost_bps: float = 20.0


class StrategyEngine:
    def __init__(self, spec, ledger, history_store=None):
        self.spec, self.ledger, self.history_store = spec, ledger, history_store

    def run_intraday(self, snapshot):
        history = self.history_store.before(snapshot.trade_date) if self.history_store else []
        regime = self.spec.regime.detect(snapshot, history)
        selection = self.spec.selector.select(snapshot)
        intents = self.spec.policy.plan(snapshot, regime, selection, self.ledger.positions)
        executed = self.ledger.execute_intraday(snapshot.trade_date, intents, snapshot.quotes_0945)
        if self.history_store:
            self.history_store.append({**snapshot.regime_raw, 'trade_date': snapshot.trade_date, 'time': '09:45'})
        return {'strategy': self.spec.name, 'trade_date': snapshot.trade_date, 'regime': asdict(regime), 'selection': asdict(selection), 'intents': [asdict(i) for i in intents], 'execution': executed}

    def run_eod(self, trade_date, close_prices):
        return self.ledger.mark_eod(trade_date, close_prices)


def default_strategy_specs():
    selector = MainThemeNormalizedAccelSelector()
    return (StrategySpec('main_a_p2', WeatherSoloRegime(), selector, SyncSwitchPortfolioPolicy()),
            StrategySpec('main_a_p2_lu_hold', WeatherSoloRegime(), selector, SyncSwitchPortfolioPolicy(True)),
            StrategySpec('baseline_pos20', Pos20Regime(), selector, SyncSwitchPortfolioPolicy()))
