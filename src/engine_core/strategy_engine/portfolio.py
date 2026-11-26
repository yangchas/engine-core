"""Intent generation; no broker or accounting dependencies."""
from dataclasses import dataclass
from .contracts import OrderIntent


@dataclass(frozen=True)
class SyncSwitchPortfolioPolicy:
    hold_if_limit_up: bool = False

    def plan(self, snapshot, regime, selection, positions):
        intents = []
        for pos in positions:
            if pos.buy_date >= snapshot.trade_date:
                continue
            q = snapshot.quotes_0945.get(pos.symbol, {})
            ap = q.get('ap1_milli', q.get('ap1'))
            bp = q.get('bp1_milli', q.get('bp1'))
            av = q.get('av1')
            if self.hold_if_limit_up and (ap is None or ap <= 0 or (av is not None and av <= 0)) and bp is not None and bp > 0 and av is not None:
                continue
            intents.append(OrderIntent('SELL', pos.symbol, reason='T1_SYNC_EXIT'))
        if regime.target_exposure > 0 and selection.picks:
            weight = min(regime.target_exposure / len(selection.picks), regime.max_single_weight)
            intents.extend(OrderIntent('BUY', sym, weight, ask, regime.label) for sym, _, ask, _ in selection.picks)
        return tuple(intents)
