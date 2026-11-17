"""Derive explainable theme relationships and auction cards from caller-supplied facts."""
from collections import defaultdict
from statistics import mean

from .data_pipeline.auction_seal_facts import classify_limit

STYLE_WORDS = ('北交所', '可转债', '破净', '高股息', '中字头', '超跌', '融资融券',
               '沪股通', '深股通', 'MSCI', '沪深300', '上证50', '中证500',
               'ST板块', '科创板', '创业板', '深证成指', '上证指数')
GEOGRAPHY_SUFFIXES = ('省', '市', '自治区', '自贸区', '特别行政区')
VERSION = 'theme_taxonomy_v1'


def _name_overlap(child_name, parent_name):
    """Return a deterministic longest common contiguous substring, if meaningful."""
    if not child_name or not parent_name:
        return None
    for length in range(min(len(child_name), len(parent_name)), 1, -1):
        matches = {child_name[i:i + length] for i in range(len(child_name) - length + 1)
                   if child_name[i:i + length] in parent_name}
        if matches:
            return min(matches)
    return None


def build_taxonomy(plate_members_by_code, plate_names=None, stock_boards=None, *, containment=0.8,
                   size_ratio=1.5, min_children_for_broad=3, min_broad_members=300):
    if not 0 < containment <= 1 or size_ratio <= 1 or min_children_for_broad < 1 or min_broad_members < 1:
        raise ValueError('invalid_taxonomy_parameters')
    names = {str(k): v for k, v in (plate_names or {}).items()}
    boards = stock_boards or {}
    members = {str(k): set(v) for k, v in plate_members_by_code.items()}
    parameters = dict(version=VERSION, containment=containment, size_ratio=size_ratio,
                      min_children_for_broad=min_children_for_broad, min_broad_members=min_broad_members)
    relations, by_child, by_parent = [], defaultdict(list), defaultdict(list)
    codes = sorted(members)
    for child in codes:
        cm = members[child]
        if not cm:
            continue
        for parent in codes:
            pm = members[parent]
            if child == parent or len(pm) < size_ratio * len(cm):
                continue
            overlap = len(cm & pm) / len(cm)
            if overlap >= containment:
                relation = dict(child=child, parent=parent, containment=overlap,
                                child_members=len(cm), parent_members=len(pm))
                relations.append(relation)
                by_child[child].append(relation)
                by_parent[parent].append(relation)
    plates = {}
    for code in codes:
        symbols = sorted(members[code])
        name = names.get(code) or ''
        reason = None
        if name.endswith(GEOGRAPHY_SUFFIXES):
            reason = 'geography_name'
        elif any(word.casefold() in name.casefold() for word in STYLE_WORDS):
            reason = 'style_name'
        elif symbols and all(boards.get(s) and boards[s] != 'UNKNOWN' for s in symbols) and len({boards[s] for s in symbols}) == 1:
            reason = 'single_market_board'
        candidates = sorted(by_child[code], key=lambda r: (r['parent_members'], -r['containment'], r['parent']))
        overlaps = {r['parent']: _name_overlap(name, names.get(r['parent'])) for r in candidates}
        named = [r for r in candidates if overlaps[r['parent']]]
        if named:
            primary = min(named, key=lambda r: (-len(overlaps[r['parent']]), r['parent_members'],
                                                -r['containment'], r['parent']))
            disambiguation, matched_name = 'name_overlap', overlaps[primary['parent']]
        elif candidates:
            primary = candidates[0]
            disambiguation = 'smallest' if name and names.get(primary['parent']) else 'none'
            matched_name = None
        else:
            primary, disambiguation, matched_name = None, 'none', None
        plates[code] = dict(plate_code=code, name=name or None, members=symbols,
                            member_count=len(symbols), non_theme_reason=reason,
                            is_broad=not reason and len(symbols) >= min_broad_members and len(by_parent[code]) >= min_children_for_broad,
                            primary_parent=primary['parent'] if primary else None,
                            secondary_parents=[r['parent'] for r in candidates if r is not primary],
                            disambiguation=disambiguation, name_overlap=matched_name)
    orphans = [c for c in codes if not plates[c]['is_broad'] and not plates[c]['non_theme_reason'] and plates[c]['primary_parent'] is None]
    return dict(parameters=parameters, plates=plates, relations=relations, orphans=orphans)


def build_theme_cards(taxonomy, auction_rows, hot_rows, seal_rows, *, top_n=10,
                      listings=None, trading_dates=None, trade_date=None):
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 0:
        raise ValueError('invalid_top_n')
    current = {}
    for row in auction_rows:
        if row.get('auction_tag', '0925') == '0924':
            continue
        if row['symbol'] in current:
            raise ValueError('duplicate_auction_symbol')
        current[row['symbol']] = row
    hot = {str(r['plate_code']): r for r in hot_rows}
    seals = {r['symbol']: r for r in seal_rows}
    amounts = [r.get('amount_yuan') for r in current.values()]
    market_amount = sum(a for a in amounts if a is not None)
    market_complete = bool(amounts) and all(a is not None for a in amounts)
    listings = listings or {}
    parameters = taxonomy['parameters']
    def card(plate):
        rows = [current[s] for s in plate['members'] if s in current and current[s].get('price_milli') is not None]
        changed = [r for r in rows if r.get('change_bp') is not None]
        ordered = sorted(changed, key=lambda r: (-r['change_bp'], r['symbol']))
        values = [r['amount_yuan'] for r in rows if r.get('amount_yuan') is not None]
        amount = sum(values) if rows and len(values) == len(rows) else None
        weighted = [r for r in changed if r.get('amount_yuan') is not None]
        weight = sum(r['amount_yuan'] for r in weighted)
        sides = [classify_limit(trade_date, r['symbol'], r, listings.get(r['symbol']), trading_dates)
                 for r in rows] if trade_date and trading_dates else []
        h = hot.get(plate['plate_code'], {})
        return dict(plate_code=plate['plate_code'], name=plate['name'], member_count=plate['member_count'],
                    non_theme_reason=plate['non_theme_reason'], taxonomy_parameters=parameters.copy(),
                    auction_count=len(rows), missing_price_count=plate['member_count']-len(rows),
                    missing_change_count=len(rows)-len(changed),
                    strong_count=sum(r['change_bp'] >= 300 for r in changed),
                    weak_count=sum(r['change_bp'] <= -300 for r in changed),
                    middle_count=sum(-300 < r['change_bp'] < 300 for r in changed),
                    limit_up_count=sides.count('limit_up') if sides else None,
                    limit_down_count=sides.count('limit_down') if sides else None,
                    unknown_limit_count=sum(s in ('unknown_listing', 'unknown_price') for s in sides) if sides else len(rows),
                    seal_up_count=sum(seals.get(r['symbol'], {}).get('side') == 'limit_up' for r in rows),
                    seal_down_count=sum(seals.get(r['symbol'], {}).get('side') == 'limit_down' for r in rows),
                    mean_change_bp=mean(r['change_bp'] for r in changed) if changed else None,
                    weighted_mean_change_bp=sum(r['change_bp']*r['amount_yuan'] for r in weighted)/weight if weight else None,
                    mean_ex_top3_bp=mean(r['change_bp'] for r in ordered[3:]) if len(ordered)>3 else None,
                    amount_top3_share=sum(sorted(values, reverse=True)[:3])/amount if amount else None,
                    amount_yuan=amount, market_amount_share=amount/market_amount if amount is not None and market_amount and market_complete else None,
                    rank=h.get('rank'), strength=h.get('strength'))
    plates = taxonomy['plates']
    all_cards = {code: card(p) for code, p in plates.items()}
    broad_cards = []
    for code, plate in plates.items():
        if not plate['is_broad']:
            continue
        children = [all_cards[c] for c, p in plates.items() if p['primary_parent'] == code and not p['is_broad'] and not p['non_theme_reason']]
        result = dict(all_cards[code])
        result['top_amount'] = sorted(children, key=lambda c: (c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code']))[:top_n]
        result['top_strength'] = sorted(children, key=lambda c: (c['mean_change_bp'] is None, -(c['mean_change_bp'] or 0), c['plate_code']))[:top_n]
        broad_cards.append(result)
    return dict(broad_cards=broad_cards, orphans=[all_cards[c] for c in taxonomy['orphans']],
                non_theme=[all_cards[c] for c, p in plates.items() if p['non_theme_reason']])
