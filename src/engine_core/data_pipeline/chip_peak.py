"""Vectorized chip distribution calculation over independent price windows."""
import numpy as np

FIELDS = ('peak_price', 'peak_weight_pct', 'chip_percent', 'avg_cost',
          'profit_ratio', 'loss_ratio', 'cost_low_70', 'cost_high_70',
          'concentration_70', 'dense_bins')


def calculate_windows(prices, turnovers):
    """Calculate one distribution per ascending, already adjusted price window."""
    if len(prices) != len(turnovers):
        raise ValueError('window count mismatch')
    results = []
    valid = []
    for index, (p, t) in enumerate(zip(prices, turnovers, strict=True)):
        if len(p) != len(t) or len(p) > 120:
            raise ValueError('invalid window length')
        result = dict.fromkeys(FIELDS)
        result.update(bars_used=len(p), quality='PARTIAL', warm=False)
        results.append(result)
        try:
            pp = np.array(p, dtype=float)
            tt = np.array(t, dtype=float)
            ok = len(p) >= 5 and np.isfinite(pp).all() and np.isfinite(tt).all() and (tt >= 0).all()
        except (TypeError, ValueError):
            ok = False
        if not ok:
            continue
        lo, hi = float(pp.min()), float(pp.max())
        if lo == hi:
            result.update(peak_price=lo, concentration_70=0, dense_bins=1)
            continue
        valid.append((index, pp, tt, lo, hi))
    if not valid:
        return results
    n = len(valid)
    lo = np.array([v[3] for v in valid]); hi = np.array([v[4] for v in valid])
    p = np.repeat(lo[:, None], 120, axis=1)
    t = np.zeros((n, 120))
    for i, (_, pp, tt, _, _) in enumerate(valid):
        p[i, -len(pp):] = pp
        t[i, -len(tt):] = tt
    edges = np.array([np.linspace(a, b, 51) for a, b in zip(lo, hi, strict=True)])
    centers = (edges[:, :-1] + edges[:, 1:]) / 2
    weights = np.zeros((n, 50))
    rows = np.arange(n)
    for step in range(120):
        turn = np.minimum(t[:, step] / 100, 1)
        weights *= (1 - turn[:, None])
        bins = np.minimum(((p[:, step] - lo) / (hi - lo) * 50).astype(int), 49)
        weights[rows, bins] += turn
    cumulative = np.cumsum(weights, axis=1)
    total = cumulative[:, -1]
    peak = np.argmax(weights, axis=1)
    for i, (index, pp, _, _, _) in enumerate(valid):
        out = results[index]
        peak_price = round(float(centers[i, peak[i]]), 2)
        if total[i] <= 0:
            out.update(peak_price=peak_price, peak_weight_pct=0, chip_percent=0,
                       avg_cost=peak_price, profit_ratio=0, loss_ratio=0,
                       cost_low_70=peak_price, cost_high_70=peak_price,
                       concentration_70=1, dense_bins=0)
            continue
        percent = round(float(weights[i, peak[i]] / total[i] * 100), 2)
        cost = np.cumsum(centers[i] * weights[i])[-1]
        profit = np.cumsum(np.where(centers[i] <= pp[-1], weights[i], 0))[-1]
        ratio = round(float(profit / total[i]), 4)
        low = min(int(np.searchsorted(cumulative[i], total[i] * .15)), 49)
        high = min(int(np.searchsorted(cumulative[i], total[i] * .85)), 49)
        ranked = np.cumsum(np.sort(weights[i])[::-1])
        rank = min(int(np.searchsorted(ranked, total[i] * .7)), 49)
        out.update(peak_price=peak_price, peak_weight_pct=percent, chip_percent=percent,
                   avg_cost=round(float(cost / total[i]), 2), profit_ratio=ratio,
                   loss_ratio=round(max(0, 1 - ratio), 4),
                   cost_low_70=round(float(centers[i, low]), 2),
                   cost_high_70=round(float(centers[i, high]), 2),
                   concentration_70=round((rank + 1) / 50, 4),
                   dense_bins=int(np.count_nonzero(weights[i] > total[i] * .05)))
        if len(pp) == 120:
            out.update(quality='FORMAL', warm=True)
    return results
