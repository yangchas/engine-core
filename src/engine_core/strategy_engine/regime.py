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
