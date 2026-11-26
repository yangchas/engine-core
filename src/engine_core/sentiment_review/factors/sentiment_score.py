from ._common import mean

def compute_sentiment_temperature(current, history=(), *, time=None, previous_0925=None, window=60, min_periods=20):
    """Percentile rank against the current and prior observations at the same cut.

    Required raw pillars: up_ratio, net_lu, seal_rate, yest_lu_avg_pct,
    downside_count. Missing pillars propagate instead of assuming neutrality.
    history items may carry a 'time' field; when time is specified it must match.
    """
    keys = ('up_ratio','net_lu','seal_rate','yest_lu_avg_pct','downside_count')
    hist = [h for h in history if time is None or h.get('time') == time][-max(window-1,0):] if window > 1 else []
    sample = hist + [current]
    pillars = {}
    for key in keys:
        val = current.get(key)
        values = [h[key] for h in sample if h.get(key) is not None]
        rank = 100*sum(x <= val for x in values)/len(values) if val is not None and len(values) >= min_periods else None
        pillars[key] = 100-rank if rank is not None and key == 'downside_count' else rank
    temp = mean(list(pillars.values())) if all(v is not None for v in pillars.values()) else None
    delta = temp-previous_0925 if temp is not None and previous_0925 is not None else None
    if temp is None: regime = None
    elif temp <= 10: regime = '双冰点' if delta is None or delta <= 0 else '假冰点'
    elif temp < 30: regime = '偏冷'
    elif temp < 60: regime = '中性'
    elif temp < 80: regime = '活跃'
    else: regime = '高潮'
    return {'temperature': temp, 'pillars': pillars, 'delta_25_45': delta, 'regime': regime}
