"""Offline, in-memory timeline builder. Input availability is a caller contract."""
from dataclasses import asdict
from .contracts import DatasetMeta
from .factors import (compute_market_global_factor, compute_ladder_relay_factor,
    compute_broken_rotten_factor, compute_top_amount_core_factor, compute_extremes_factor,
    compute_auction_themes_5d_factor, compute_kaipan_hot_factor, compute_sentiment_temperature)

def build_timeseries_dataset(days, *, min_periods=None, min_history=10):
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
        has_ticks = any(rows for cut, rows in record.get('snapshots', {}).items() if cut != '15:00')
        cohorts = record.get('prior_official_cohorts') or {}
        active_streaks = cohorts.get('official_streaks')
        active_broken = cohorts.get('official_broken_symbols')
        floor = cohorts.get('official_floor_symbols')
        cross_check = {'limit_up': len(streaks), 'broken': len(prior_broken),
                       'rotten': len(prior_rotten)}
        active_streaks = streaks if active_streaks is None else active_streaks
        active_broken = prior_broken if active_broken is None else active_broken
        today_snaps = {}
        temp25 = None
        close_ext = None
        close_rows = None
        for cut, rows in sorted(record.get('snapshots', {}).items()):
            if cut != '15:00' and not rows:
                continue
            today_snaps[cut] = rows
            ext = compute_extremes_factor(rows)
            market = compute_market_global_factor(rows, amount_history.get(cut, ()))
            ladder = compute_ladder_relay_factor(rows, active_streaks, ext['sets']['sealed_up'])
            broken = compute_broken_rotten_factor(rows, active_broken, prior_rotten, ext['sets']['sealed_up'])
            top = compute_top_amount_core_factor(rows, prior_rows, ext['sets']['sealed_up'])
            from .factors._common import feedback
            ladder['cohort_source'] = 'kaipan_hist_cache' if cohorts.get('official_streaks') is not None else 'daily_kline_v1'
            broken['cohort_source'] = 'kaipan_hist_cache' if cohorts.get('official_broken_symbols') is not None else 'daily_kline_v1'
            broken['rotten_cohort_source'] = 'daily_kline_v1'
            broken['floor'] = feedback(floor, rows, ext['sets']['sealed_up']) if floor is not None else None
            broken['floor_cohort_source'] = 'kaipan_hist_cache' if floor is not None else None
            all_lu = list(active_streaks)
            from .factors._common import feedback
            ylu = feedback(all_lu, rows)
            raw = {'time': cut, 'up_ratio': market['up_ratio'], 'net_lu': ext['sealed_up']-ext['sealed_down'],
                   'seal_rate': ext['seal_rate'], 'yest_lu_avg_pct': ylu['avg_pct'],
                   'downside_count': ext['big_noodle'] if cut == '15:00' else ext['nuke_5pct']}
            score = compute_sentiment_temperature(raw, histories.get(cut, ()), time=cut,
                previous_0925=temp25 if cut == '09:45' else None, min_periods=min_periods if min_periods is not None else (20 if cut == '15:00' else min_history + 1))
            if cut == '09:25': temp25 = score['temperature']
            themes = compute_auction_themes_5d_factor(record.get('members', {}),
                {k:v for k,v in today_snaps.items() if k in ('09:25','09:35','09:45')},
                theme_history, record.get('prior_features', {}))
            # Only include captures explicitly available no later than the snapshot.
            hot = record.get('kaipan', {})
            today_hot = hot.get(cut)
            kaipan = compute_kaipan_hot_factor(hot.get('prev_close'),
                today_hot if cut == '09:25' else None, today_hot if cut == '15:00' else None,
                today_0945=today_hot if cut == '09:45' else None, today_0935=today_hot if cut == '09:35' else None)
            if cut != '15:00':
                from engine_core.auction_theme_buckets import normalize_theme_label
                from .factors._common import pct, mean, ratio
                names = {normalize_theme_label(x) for x in (kaipan['prev_close'] or [])}
                symbols = {s for name, members in record.get('hot_members', record.get('members', {})).items() if normalize_theme_label(name) in names for s in members}
                present = [rows[s] for s in symbols if s in rows and pct(rows[s]) is not None]
                vals = [pct(r) for r in present]
                amounts = [r['amount_yuan'] for r in present if r.get('amount_yuan') is not None]
                kaipan['prev_hot_member_perf'] = {'avg_pct': mean(vals), 'red_pct': ratio(sum(v > 0 for v in vals), len(vals)), 'tot_amt_yi': sum(amounts)/1e8 if amounts else None, 'n_symbols': len(vals)} if symbols else None
            clean_ext = {k:v for k,v in ext.items() if k != 'sets'}
            factors = {'market_global': market, 'ladder_relay': ladder, 'broken_rotten': broken,
                       'top_amount_core': top, 'extremes': clean_ext, 'auction_themes_5d': themes,
                       'kaipan_hot': kaipan, 'sentiment_score': score}
            points.append({'trade_date': day, 'time': cut, 'factors': factors})
            if cut == '15:00' or has_ticks:
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
        intraday.append({'trade_date': day, 'previous_date': previous_date, 'has_intraday_ticks': has_ticks,
                         'cohort_cross_check': {'daily_kline_v1': cross_check,
                           'kaipan_hist_cache': {'limit_up': len(cohorts['official_streaks']) if cohorts.get('official_streaks') is not None else None,
                           'broken': len(cohorts['official_broken_symbols']) if cohorts.get('official_broken_symbols') is not None else None,
                           'floor': len(floor) if floor is not None else None}}, 'timepoints': points})
        previous_date = day
    meta = DatasetMeta(daily_days=len(daily), intraday_days=sum(d['has_intraday_ticks'] for d in intraday),
                       intraday_tick_days=sum(d['has_intraday_ticks'] for d in intraday))
    return {'meta': asdict(meta), 'daily': daily, 'intraday': intraday}
