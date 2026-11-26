from ._common import feedback

def compute_top_amount_core_factor(rows, previous_rows, sealed_up=None):
    eligible = [(s,r['amount_yuan']) for s,r in previous_rows.items() if r.get('amount_yuan') is not None]
    symbols = [s for s,_ in sorted(eligible, key=lambda x: (-x[1], x[0]))[:100]]
    return feedback(symbols, rows, sealed_up)
