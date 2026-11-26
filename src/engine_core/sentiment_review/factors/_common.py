"""Shared numerical primitives. Missing observations remain missing."""
from statistics import fmean, median

def mean(xs):
    xs = [x for x in xs if x is not None]
    return fmean(xs) if xs else None

def med(xs):
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None

def ratio(n, d):
    return 100 * n / d if d else None

def pct(row):
    if row.get('pct') is not None:
        return row['pct']
    px, pc = row.get('px'), row.get('pc')
    return 100 * (px / pc - 1) if px is not None and pc is not None and pc > 0 else None

def feedback(symbols, rows, sealed=None):
    present = [(s, pct(rows[s])) for s in symbols if s in rows]
    present = [(s, v) for s, v in present if v is not None]
    values = [v for _, v in present]
    return {'n': len(values), 'avg_pct': mean(values),
            'red_pct': ratio(sum(v > 0 for v in values), len(values)),
            'promo_pct': ratio(sum(s in sealed for s, _ in present), len(values)) if sealed is not None else None,
            'nuke_pct': ratio(sum(v <= -5 for v in values), len(values))}
