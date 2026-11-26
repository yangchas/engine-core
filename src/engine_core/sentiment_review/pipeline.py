"""Offline, in-memory timeline builder. Input availability is a caller contract."""
from dataclasses import asdict
from .contracts import DatasetMeta
from .factors import (compute_market_global_factor, compute_ladder_relay_factor,
    compute_broken_rotten_factor, compute_top_amount_core_factor, compute_extremes_factor,
    compute_auction_themes_5d_factor, compute_kaipan_hot_factor, compute_sentiment_temperature)

def build_timeseries_dataset(days, *, min_periods=20):
    """Build from date-keyed day records.

    Each day has `snapshots` mapping HH:MM to symbol-keyed normalized rows, and
    optionally `members`, `prior_features`, and `kaipan` (time-keyed plate lists).
    15:00 is the daily close. Membership/prior_features must be known by that day;
    no adapter is called and no outcome is included in a feature snapshot.
    A snapshot is a complete as-of view, not an incremental tick batch.
    """
    dates = sorted(days)
    histories = {}
    amount_history = {}
    theme_history = {}
    prior_rows, streaks, prior_broken, prior_rotten = {}, {}, set(), set()
    daily, intraday = [], []
    previous_date = None
    for day in dates:
        record = days[day]
        points = []
        today_snaps = {}
        temp25 = None
        close_ext = None
        close_rows = None
        for cut, rows in sorted(record.get('snapshots', {}).items()):
            today_snaps[cut] = rows
            ext = compute_extremes_factor(rows)
            market = compute_market_global_factor(rows, amount_history.get(cut, ()))
            ladder = compute_ladder_relay_factor(rows, streaks, ext['sets']['sealed_up'])
            broken = compute_broken_rotten_factor(rows, prior_broken, prior_rotten)
            top = compute_top_amount_core_factor(rows, prior_rows)
            all_lu = [s for s in streaks]
            from .factors._common import feedback
            ylu = feedback(all_lu, rows)
            raw = {'time': cut, 'up_ratio': market['up_ratio'], 'net_lu': ext['sealed_up']-ext['sealed_down'],
                   'seal_rate': ext['seal_rate'], 'yest_lu_avg_pct': ylu['avg_pct'],
                   'downside_count': ext['big_noodle'] if cut == '15:00' else ext['nuke_5pct']}
            score = compute_sentiment_temperature(raw, histories.get(cut, ()), time=cut,
                previous_0925=temp25 if cut == '09:45' else None, min_periods=min_periods)
            if cut == '09:25': temp25 = score['temperature']
            themes = compute_auction_themes_5d_factor(record.get('members', {}),
                {k:v for k,v in today_snaps.items() if k in ('09:25','09:35','09:45')},
                theme_history, record.get('prior_features', {}))
            # Only include captures explicitly available no later than the snapshot.
            hot = record.get('kaipan', {})
            today_hot = hot.get(cut)
            kaipan = compute_kaipan_hot_factor(hot.get('prev_close'),
                today_hot if cut == '09:25' else None, today_hot if cut == '15:00' else None,
                today_0945=today_hot if cut == '09:45' else None)
            clean_ext = {k:v for k,v in ext.items() if k != 'sets'}
            factors = {'market_global': market, 'ladder_relay': ladder, 'broken_rotten': broken,
                       'top_amount_core': top, 'extremes': clean_ext, 'auction_themes_5d': themes,
                       'kaipan_hot': kaipan, 'sentiment_score': score}
            points.append({'trade_date': day, 'time': cut, 'factors': factors})
            histories.setdefault(cut, []).append(raw)
            if market['tot_amt_yi'] is not None: amount_history.setdefault(cut, []).append(market['tot_amt_yi'])
            if cut == '15:00': close_ext,close_rows = ext,rows
        if '09:25' in today_snaps:
            for theme, symbols in record.get('members', {}).items():
                vals = [today_snaps['09:25'][s].get('amount_yuan') for s in symbols if s in today_snaps['09:25']]
                vals = [v for v in vals if v is not None]
                if vals and sum(vals) > 0: theme_history.setdefault(theme, []).append(sum(vals))
        if close_ext is not None:
            streaks = {s: streaks.get(s,0)+1 for s in close_ext['sets']['sealed_up']}
            prior_broken,prior_rotten = close_ext['sets']['broken_up'],close_ext['sets']['rotten']
            prior_rows = close_rows
            daily.append(next(p for p in points if p['time'] == '15:00'))
        else:
            # Never treat an older close as the immediately preceding session.
            streaks, prior_broken, prior_rotten, prior_rows = {}, set(), set(), {}
        intraday.append({'trade_date': day, 'previous_date': previous_date, 'timepoints': points})
        previous_date = day
    meta = DatasetMeta(daily_days=len(daily), intraday_days=len(intraday))
    return {'meta': asdict(meta), 'daily': daily, 'intraday': intraday}
