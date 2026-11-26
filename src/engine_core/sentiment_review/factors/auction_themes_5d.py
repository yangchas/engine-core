"""Auction theme factors at an explicit cut; no future snapshot is consulted."""
from math import sqrt
from ._common import mean, med, pct, ratio

def compute_auction_themes_5d_factor(members, snapshots, prior_auction_amounts=None, prior_features=None, outcomes=None):
    """snapshots maps '09:25'/'09:35'/'09:45' to symbol rows available by cut.

    Membership and prior_features must themselves be point-in-time inputs. Outcomes,
    when supplied, are returned in a separate mapping, never in the feature rows.
    """
    prior_auction_amounts = prior_auction_amounts or {}
    prior_features = prior_features or {}
    output = []
    for theme, symbols in members.items():
        symbols = set(symbols)
        present = {s for snap in snapshots.values() for s in symbols if s in snap}
        if len(present) < 5: continue
        def vals(cut):
            return [pct(snapshots.get(cut, {})[s]) for s in present if s in snapshots.get(cut, {}) and pct(snapshots[cut][s]) is not None]
        def amount(cut):
            arr = [snapshots[cut][s].get('amount_yuan') for s in present if s in snapshots.get(cut, {})]
            arr = [v for v in arr if v is not None]
            return sum(arr) if arr else None
        a25,a35,a45 = (amount(c) for c in ('09:25','09:35','09:45'))
        if a25 is None or a25 < 3e7 and (a35 is None or a35 < 5e7): continue
        history = [x for x in list(prior_auction_amounts.get(theme, ())) [-5:] if x is not None and x > 0]
        crowd = a25 / mean(history) if len(history) >= 3 else None
        v25,v35,v45 = (vals(c) for c in ('09:25','09:35','09:45'))
        u35,u45 = (ratio(sum(v > 0 for v in vs),len(vs)) for vs in (v35,v45))
        pf = [prior_features[s] for s in present if s in prior_features]
        ma = [r['above_ma20'] for r in pf if r.get('above_ma20') is not None]
        dde = [r['dde5_yuan'] for r in pf if r.get('dde5_yuan') is not None]
        output.append({'theme': theme, 'n': len(present), 'amt25_yi': a25/1e8,
            'crowd5': crowd, 'p25': mean(v25), 'p35': mean(v35), 'p45': mean(v45),
            'up35': u35, 'up45': u45, 'bsurge': u45-u35 if u35 is not None and u45 is not None else None,
            'relay': (a45-a35)/(a35-a25) if a45 is not None and a35 is not None and a35 > a25 else None,
            'pos20': med([r.get('pos20') for r in pf]), 'ma20_pct': ratio(sum(ma),len(ma)),
            'dde5_yi': sum(dde)/1e8 if dde else None,
            '_score': a25 * (sqrt(max(.5,min(4,crowd))) if crowd is not None else 1)})
    output.sort(key=lambda r: (-r['_score'], r['theme']))
    for i,row in enumerate(output,1):
        row['auc_rank'] = i
        del row['_score']
    result = {'features': output}
    if outcomes is not None:
        result['outcomes'] = {r['theme']: outcomes.get(r['theme']) for r in output}
    return result
