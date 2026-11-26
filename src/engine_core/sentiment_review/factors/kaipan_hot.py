def compute_kaipan_hot_factor(prev_close=None, today_0925=None, today_close=None, top_n=10, today_0945=None):
    """Preserve ordered plates and compute pairwise overlap when both are known."""
    def names(rows):
        if rows is None: return None
        return [r if isinstance(r,str) else r.get('name') for r in rows[:top_n] if isinstance(r,str) or r.get('name')]
    prev, opening, close, late = map(names, (prev_close,today_0925,today_close,today_0945))
    def overlap(a,b):
        return len(set(a) & set(b)) if a is not None and b is not None else None
    return {'prev_close': prev, 'today_0925': opening, 'today_0945': late, 'today_close': close,
            'prev_to_open_overlap': overlap(prev,opening), 'open_to_close_overlap': overlap(opening,close),
            'prev_to_close_overlap': overlap(prev,close)}
