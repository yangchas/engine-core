"""Composition and dated rolling history."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from .regime import WeatherSoloRegime, MacroB1Regime, AlwaysOnBaselineRegime
from .selector import MainThemeNormalizedAccelSelector, LowPos20SectorSelector
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
    category: str = 'intraday'
    slots: tuple[str, ...] = ('0945', '1810')
    enabled: bool = True


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
        names = {}
        for candidate in snapshot.candidates:
            symbol = candidate.get('symbol') if isinstance(candidate, dict) else candidate.symbol
            name = candidate.get('name') if isinstance(candidate, dict) else candidate.name
            if symbol and name:
                names[symbol] = name
        return {'strategy': self.spec.name, 'trade_date': snapshot.trade_date, 'regime': asdict(regime), 'selection': asdict(selection), 'intents': [asdict(i) for i in intents], 'execution': executed, 'stock_names': names}

    def run_eod(self, trade_date, close_prices):
        return self.ledger.mark_eod(trade_date, close_prices)


def all_strategy_specs():
    old = MainThemeNormalizedAccelSelector()
    return (StrategySpec('b1_low_pos20', MacroB1Regime(), LowPos20SectorSelector(), SyncSwitchPortfolioPolicy(), category='macro'),
            StrategySpec('b1_dual_green', MacroB1Regime(True, .30), LowPos20SectorSelector(prefer_up45_in_bear=True), SyncSwitchPortfolioPolicy()),
            StrategySpec('baseline_low_pos20', AlwaysOnBaselineRegime(), LowPos20SectorSelector(), SyncSwitchPortfolioPolicy(), category='baseline'),
            StrategySpec('main_a_p2', WeatherSoloRegime(), old, SyncSwitchPortfolioPolicy(), enabled=False),
            StrategySpec('main_a_p2_lu_hold', WeatherSoloRegime(), old, SyncSwitchPortfolioPolicy(True), enabled=False))


def default_strategy_specs():
    return tuple(spec for spec in all_strategy_specs() if spec.enabled)


def resolve_strategy_specs(config_path=None):
    specs = all_strategy_specs()
    if not config_path or not Path(config_path).exists():
        return tuple(spec for spec in specs if spec.enabled)
    cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError('strategies config must be a JSON object')
    active = []
    for spec in specs:
        if spec.name in cfg:
            val = cfg[spec.name]
            enabled = bool(val.get('enabled')) if isinstance(val, dict) else bool(val)
        else:
            enabled = spec.enabled
        if enabled:
            active.append(spec)
    return tuple(active)
