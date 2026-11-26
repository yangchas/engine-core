"""Prior-session ladder structure evaluated using a single current snapshot."""
from ._common import feedback


def compute_leader_divergence_factor(previous_streaks, rows, sealed_up):
    maximum = max((n for n in previous_streaks.values() if n > 0), default=0)
    leaders = sorted(s for s, n in previous_streaks.items() if n == maximum and n > 0)
    leader = feedback(leaders, rows, sealed_up)
    high = feedback([s for s, n in previous_streaks.items() if n >= 3], rows)['avg_pct']
    mid = feedback([s for s, n in previous_streaks.items() if n == 2], rows)['avg_pct']
    low = feedback([s for s, n in previous_streaks.items() if n == 1], rows)['avg_pct']
    avg, promo, nuke = leader['avg_pct'], leader['promo_pct'], leader['nuke_pct']
    if maximum >= 3 and ((promo is not None and promo >= 50) or (avg is not None and avg >= 3)) and (low is None or low >= 0):
        label = '龙头打高度'
    elif ((nuke is not None and nuke >= 50) or (avg is not None and avg <= -2)) and low is not None and low > .5:
        label = '高低切'
    elif avg is not None and avg > 0 and low is not None and low > 0 and mid is not None and mid < -1:
        label = '淘汰中位'
    elif avg is not None and avg < 0 and low is not None and low < 0:
        label = '梯队通杀'
    else:
        label = '常规分化'
    return {'max_h': maximum, 'leader_count': len(leaders), 'leader_symbols': leaders,
            'leader_avg_pct': avg, 'leader_promo_pct': promo, 'leader_nuke_pct': nuke,
            'high_3p_avg_pct': high, 'mid_2b_avg_pct': mid, 'low_1b_avg_pct': low,
            'high_minus_low_pp': high-low if high is not None and low is not None else None,
            'mid_minus_low_pp': mid-low if mid is not None and low is not None else None,
            'structure_label': label}
