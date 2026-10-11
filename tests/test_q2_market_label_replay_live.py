"""STAR-market label difference between the TD replay path and the live feed.

Real 2026-10-09 comparison (task009-f): for all 618 compared 688xxx symbols the deployed t1-v2 TD
replay path labelled the market ``kc`` (derived from the symbol prefix because TD rows carry no
market/exchange column) while the live Q2 hashes carried ``sh``.  Core's equity filter must not
depend on which of the two labels reaches it.
"""

from engine_core.q2 import classify_equity


def test_star_label_difference_between_td_replay_and_live_is_equity_invariant():
    for symbol in ("688001", "688981", "689009"):
        assert classify_equity(symbol, {"mk": "kc", "px": "1000"}) is True
        assert classify_equity(symbol, {"mk": "sh", "px": "1000"}) is True


def test_kc_label_does_not_admit_a_main_board_code():
    assert classify_equity("600000", {"mk": "kc", "px": "1000"}) is False
