from ._common import feedback

def compute_ladder_relay_factor(rows, previous_streaks, sealed_up=None):
    """Evaluate previous-session streak cohorts against the current snapshot."""
    maximum = max(previous_streaks.values(), default=0)
    groups = {'1b': [s for s,n in previous_streaks.items() if n == 1],
              '2p': [s for s,n in previous_streaks.items() if n >= 2],
              '3p': [s for s,n in previous_streaks.items() if n >= 3],
              'max_h': [s for s,n in previous_streaks.items() if n == maximum and n > 0]}
    return {k: feedback(v, rows, sealed_up) for k,v in groups.items()}
