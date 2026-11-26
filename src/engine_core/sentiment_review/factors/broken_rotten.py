from ._common import feedback

def compute_broken_rotten_factor(rows, previous_broken, previous_rotten, sealed_up=None):
    return {name: feedback(group, rows, sealed_up) for name,group in [('broken', previous_broken), ('rotten', previous_rotten)]}
