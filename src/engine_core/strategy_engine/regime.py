"""Regime classifiers, independent of selection and execution."""
from dataclasses import dataclass
from math import isfinite
from engine_core.sentiment_review.factors.sentiment_score import compute_sentiment_temperature
from .contracts import RegimeDecision


@dataclass(frozen=True)
class WeatherSoloRegime:
    gap_threshold: float = 0.60
    temp_threshold: float = 45.0
    top100_floor: float = -1.0
    window: int = 60
    min_periods: int = 11

    def detect(self, snapshot, history=()):
        prior = [{**r, 'time': r.get('time', '09:45')} for r in history if r.get('trade_date', '') < snapshot.trade_date and r.get('trade_date')]
        prior.sort(key=lambda r: r['trade_date'])
        raw = dict(snapshot.regime_raw)
        score = compute_sentiment_temperature(raw, prior, time='09:45', window=self.window, min_periods=self.min_periods)
        temp, gap, top = score['temperature'], raw.get('gap'), raw.get('top100_avg_pct')
        metrics = {'temperature': temp, 'gap': gap, 'top100_avg_pct': top, 'history_n': score['history_n']}
        if temp is None:
            return RegimeDecision('RED', 0, 0, metrics, ('WARMUP_INCOMPLETE',))
        if not all(isinstance(v, (int, float)) and isfinite(v) for v in (gap, top)):
            return RegimeDecision('RED', 0, 0, metrics, ('MISSING_REGIME_INPUT',))
        if gap < self.gap_threshold:
            return RegimeDecision('CROWDED', 0, 0, metrics, ('GAP_BELOW_THRESHOLD',))
        if temp >= self.temp_threshold and top > self.top100_floor:
            return RegimeDecision('WARM_SOLO', 1, .20, metrics, ())
        return RegimeDecision('COLD_SOLO', .50, .10, metrics, ())


@dataclass(frozen=True)
class Pos20Regime:
    threshold: float = 30.0

    def detect(self, snapshot, history=()):
        value = snapshot.regime_raw.get('pos20_med')
        if not isinstance(value, (int, float)) or not isfinite(value):
            return RegimeDecision('COLD', 0, 0, {'pos20_med': None}, ('MISSING_POS20',))
        warm = value >= self.threshold
        return RegimeDecision('WARM' if warm else 'COLD', 1 if warm else 0, .20 if warm else 0, {'pos20_med': value}, ())


def compute_macro_b1_series(bars_by_date):
    """Compute the equal-weight index and hysteresis using only dated bars."""
    from collections import defaultdict, deque
    from decimal import Decimal, ROUND_HALF_UP
    from statistics import median
    closes = defaultdict(lambda: deque(maxlen=20))
    ranges = defaultdict(lambda: deque(maxlen=20))
    components_history = deque(maxlen=60)
    indices = deque(maxlen=20)
    index, state, initialized = 1.0, 'BEAR', False
    result = []
    for day in sorted(bars_by_date):
        returns, above, positions = [], [], []
        lu = broken = floor = up = count = 0
        for symbol, bar in bars_by_date[day].items():
            if not str(symbol).startswith(('00', '60', '300', '301')):
                continue
            close = bar.get('close')
            if not isinstance(close, (int, float)) or not isfinite(close) or close <= 0:
                continue
            prior = bar.get('pre_close')
            high, low = bar.get('high'), bar.get('low')
            valid = lambda x: isinstance(x, (int, float)) and isfinite(x) and x > 0
            ranges[symbol].append((high if valid(high) else None, low if valid(low) else None))
            if valid(prior):
                count += 1
                band = Decimal('0.20') if str(symbol).startswith(('300', '301')) else Decimal('0.10')
                pre = Decimal(str(prior))
                upper = float((pre * (1 + band)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                lower = float((pre * (1 - band)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                lu += close >= upper - .015
                broken += valid(high) and high >= upper - .015 and close < upper - .015
                floor += close <= lower + .015
                up += close > prior
                window = ranges[symbol]
                if len(window) == 20 and all(h is not None and l is not None for h, l in window):
                    hi, lo = max(h for h, _ in window), min(l for _, l in window)
                    if hi > lo:
                        positions.append(100 * (close - lo) / (hi - lo))
            if not isinstance(prior, (int, float)) or not isfinite(prior) or prior <= 0:
                prior = closes[symbol][-1] if closes[symbol] else None
            if isinstance(prior, (int, float)) and isfinite(prior) and prior > 0:
                returns.append(close / prior - 1)
            closes[symbol].append(close)
            if len(closes[symbol]) == 20:
                above.append(close > sum(closes[symbol]) / 20)
        if not returns:
            continue
        ret = sum(returns) / len(returns)
        index *= 1 + ret
        indices.append(index)
        ma = sum(indices) / 20 if len(indices) == 20 else None
        pct = 100 * sum(above) / len(above) if above else None
        components = (lu if count else None, broken if count else None, floor if count else None,
                      100 * up / count if count else None, median(positions) if positions else None, pct)
        ranks = []
        for i, value in enumerate(components):
            past = [row[i] for row in components_history if row[i] is not None]
            if value is None or len(past) < 20:
                ranks.append(None)
                continue
            rank = 100 * (sum(x < value for x in past) + .5 * sum(x == value for x in past)) / len(past)
            ranks.append(100 - rank if i in (1, 2) else rank)
        eod_temp = sum(ranks) / 6 if all(x is not None for x in ranks) else None
        components_history.append(components)
        if ma is not None:
            if not initialized:
                state, initialized = ('BULL' if index > ma else 'BEAR'), True
            elif pct is not None:
                if state == 'BEAR' and index > ma and pct >= 55:
                    state = 'BULL'
                elif state == 'BULL' and index < ma and pct <= 45:
                    state = 'BEAR'
        result.append({'trade_date': day, 'mkt_ew_ret': ret, 'mkt_ew_idx': index,
                       'mkt_ma20': ma, 'above_ma20_pct': pct, 'n_stocks': len(returns), 'macro_state': state,
                       'eod_temp': eod_temp})
    return result


@dataclass(frozen=True)
class MacroB1Regime:
    allow_bear_green: bool = False
    bear_green_exposure: float = .30
    allow_bear_ice: bool = False
    bear_ice_temp_max: float = 25.0
    bear_ice_exposure: float = 1.0

    def detect(self, snapshot, history=()):
        raw = snapshot.regime_raw
        prior = sorted((dict(r, time=r.get('time', '09:45')) for r in history
                        if r.get('trade_date') and r['trade_date'] < snapshot.trade_date), key=lambda r: r['trade_date'])
        score = compute_sentiment_temperature(raw, prior, time='09:45', window=60, min_periods=11)
        temp, top, down = score['temperature'], raw.get('top100_avg_pct'), raw.get('live_limit_down')
        metrics = {'temperature': temp, 'top100_avg_pct': top, 'live_limit_down': down,
                   'macro_state': raw.get('macro_state'), 'history_n': score['history_n']}
        valid = lambda x: isinstance(x, (int, float)) and isfinite(x)
        if raw.get('macro_state') == 'BULL':
            if (valid(top) and top <= -1.5) or (valid(down) and down >= 25) or (valid(temp) and temp < 30):
                return RegimeDecision('BULL_EMERGENCY_EXIT', 0, 0, metrics, ('EMERGENCY_BRAKE',))
            return RegimeDecision('BULL', 1, .20, metrics, ())
        if raw.get('macro_state') == 'BEAR':
            ice = raw.get('prev_eod_temp')
            if self.allow_bear_ice:
                metrics['prev_eod_temp'] = ice
            if self.allow_bear_ice and valid(ice) and ice <= self.bear_ice_temp_max:
                return RegimeDecision('BEAR_ICE', self.bear_ice_exposure, .20, metrics, ())
            if self.allow_bear_green and valid(temp) and 45 <= temp < 60 and valid(top) and top > -1:
                return RegimeDecision('BEAR_GREEN', self.bear_green_exposure, self.bear_green_exposure / 5, metrics, ())
            return RegimeDecision('BEAR_CASH', 0, 0, metrics, ('MACRO_BEAR',))
        return RegimeDecision('BEAR_CASH', 0, 0, metrics, ('MISSING_MACRO_STATE',))


@dataclass(frozen=True)
class AlwaysOnBaselineRegime:
    def detect(self, snapshot, history=()):
        return RegimeDecision('BASELINE_ON', 1, .20, {}, ())


@dataclass(frozen=True)
class BearIceRegime:
    temp_max: float = 25.0
    target_exposure: float = .50
    max_single_weight: float = .10

    def detect(self, snapshot, history=()):
        temp = snapshot.regime_raw.get('prev_eod_temp')
        metrics = {'prev_eod_temp': temp}
        if isinstance(temp, (int, float)) and isfinite(temp) and temp <= self.temp_max:
            return RegimeDecision('BEAR_ICE', self.target_exposure, self.max_single_weight, metrics, ())
        return RegimeDecision('BEAR_CASH', 0, 0, metrics, ('ICE_NOT_OBSERVED',))


@dataclass(frozen=True)
class AuctionAlwaysOnRegime:
    target_exposure: float = 1.0
    max_single_weight: float = .50

    def detect(self, snapshot, history=()):
        return RegimeDecision('AUCTION_ON', self.target_exposure, self.max_single_weight, {}, ())
