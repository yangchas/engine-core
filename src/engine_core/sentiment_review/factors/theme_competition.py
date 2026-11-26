"""Rank current theme observations without accessing market data."""
from engine_core.auction_theme_buckets import normalize_theme_label


def compute_theme_competition_factor(features, prev_close_hot_names, *, time, open_0925_leader_theme=None):
    keys = {'09:25': ('p25', 'amt25_yi'), '09:35': ('p35', 'amt35_yi'), '09:45': ('p45', 'amt45_yi')}
    if time not in keys:
        raise ValueError('unsupported theme competition cut')
    pk, ak = keys[time]
    eligible = [r for r in features if r.get('n', 0) >= 5 and r.get(pk) is not None and r.get('theme')]
    eligible.sort(key=lambda r: (-r[pk], r['theme']))
    leader = eligible[0] if eligible else None
    runner = eligible[1] if len(eligible) > 1 else None
    gap = leader[pk]-runner[pk] if runner else None
    amounts = [r.get(ak) for r in eligible]
    share = (100 * sum(r[ak] for r in eligible[:3]) / sum(amounts)
             if eligible and all(a is not None and a >= 0 for a in amounts) and sum(amounts) > 0 else None)
    new = (normalize_theme_label(leader['theme']) not in {normalize_theme_label(n) for n in prev_close_hot_names[:10]}
           if leader and prev_close_hot_names is not None else None)
    takeover = (normalize_theme_label(leader['theme']) != normalize_theme_label(open_0925_leader_theme)
                if time != '09:25' and leader and open_0925_leader_theme is not None else False if time == '09:25' else None)
    label = '电风扇混战'
    if leader and leader[pk] >= 1 and gap is not None and gap >= .5 and new is not None:
        label = '新题材卡位' if new else '主线胜出'
    return {'leader_theme': leader['theme'] if leader else None, 'leader_pct': leader[pk] if leader else None,
            'runner_up_theme': runner['theme'] if runner else None, 'runner_up_pct': runner[pk] if runner else None,
            'top1_vs_top2_gap_pp': gap, 'top3_amt_share_pct': share, 'is_new_theme_breakout': new,
            'is_intraday_takeover': takeover, 'competition_label': label}
