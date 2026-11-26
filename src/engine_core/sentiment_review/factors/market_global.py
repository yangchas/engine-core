from ._common import mean, med, pct, ratio

def compute_market_global_factor(rows, prior_amounts=()):
    """Rows keyed by symbol: pct or px/pc, amount_yuan, float_cap, pos20, ma20, chip_profit_ratio."""
    values = [pct(r) for r in rows.values()]
    values = [v for v in values if v is not None]
    weighted = [(pct(r), r.get('float_cap')) for r in rows.values()]
    weighted = [(v, w) for v, w in weighted if v is not None and w is not None and w > 0]
    cap = sum(v*w for v,w in weighted) / sum(w for _,w in weighted) if weighted else None
    avg = mean(values)
    amounts = [r['amount_yuan'] for r in rows.values() if r.get('amount_yuan') is not None]
    amount = sum(amounts)/1e8 if amounts else None
    baseline = [x for x in list(prior_amounts)[-5:] if x is not None]
    base = mean(baseline) if len(baseline) >= 3 else None
    flags = [r['px'] >= r['ma20'] for r in rows.values() if r.get('px') is not None and r.get('ma20') is not None and r['ma20'] > 0]
    return {'eq_pct': avg, 'cap_w_pct': cap, 'yw_pp': avg-cap if avg is not None and cap is not None else None,
            'up_ratio': ratio(sum(v > 0 for v in values), len(values)), 'med_pct': med(values),
            'tot_amt_yi': amount, 'amt_vs_5d': amount/base if amount is not None and base else None,
            'pos20_med': med([r.get('pos20') for r in rows.values()]), 'above_ma20_pct': ratio(sum(flags), len(flags)),
            'chip_prof_med': med([r['chip_profit_ratio']*100 for r in rows.values() if r.get('chip_profit_ratio') is not None])}
