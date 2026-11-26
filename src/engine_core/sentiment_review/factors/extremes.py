from ._common import pct, ratio

def compute_extremes_factor(rows):
    """Ticks use px/pc/high in yuan, bid/ask prices in yuan, bid/ask volume in shares.

    Alternatively daily rows use open/high/low/px/pc with no order book.
    Internal symbol sets are returned for use in subsequent cohort calculations.
    """
    up, down, broken, rotten = set(), set(), set(), set()
    nuke = noodle = 0
    seal_amt = 0.0
    seal_known = False
    for s,r in rows.items():
        v = pct(r)
        if v is not None and v <= -5: nuke += 1
        px, pc, high = r.get('px'), r.get('pc'), r.get('high')
        if not px or not pc or high is None or s.startswith(('8','4','92')): continue
        band = .2 if s.startswith(('300','301','688','689')) else .1
        lu, ld = round(pc*(1+band),2), round(pc*(1-band),2)
        low, op = r.get('low'), r.get('open')
        if low is not None and v is not None and ((abs(px-round(pc*1.05,2)) <= .01 and high == px and v < 5.3) or (abs(px-round(pc*.95,2)) <= .01 and low == px and v > -5.3)): continue
        book = 'bid_price' in r or 'ask_price' in r
        if book:
            su = px >= lu-.01 and (r.get('ask_price') == 0 or r.get('ask_volume') == 0) and r.get('bid_price') is not None and r['bid_price'] >= lu-.01
            sd = px <= ld+.01 and r.get('bid_price') == 0
        else:
            su, sd = px >= lu-.015, px <= ld+.015
        if su:
            up.add(s)
            if low is not None and op is not None and low < px*.985 and op < px*.995: rotten.add(s)
            if book and r.get('bid_volume') is not None:
                seal_amt += r['bid_price']*r['bid_volume']/1e8
                seal_known = True
        elif high >= lu-(.01 if book else .015): broken.add(s)
        if sd: down.add(s)
        if v is not None and (high-px)/pc*100 >= 7 and v < 3: noodle += 1
    return {'sealed_up': len(up), 'sealed_down': len(down), 'broken_up': len(broken),
            'seal_rate': ratio(len(up),len(up)+len(broken)), 'nuke_5pct': nuke,
            'seal_amt_yi': seal_amt if seal_known else None, 'big_noodle': noodle,
            'sets': {'sealed_up': up, 'sealed_down': down, 'broken_up': broken, 'rotten': rotten}}
