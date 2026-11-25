"""C577 mutation checks: each deliberately broken builder must be caught by the real-data ladder tests."""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import pytest
import test_ladder_daily as T

from engine_core.data_pipeline import ladder_daily as ld

CHECKS = [T.test_incident_symbols_are_right_on_1027, T.test_missing_kline_day_falls_back_to_kaipan_for_that_day_only,
          T.test_continuity_rule_not_just_presence, T.test_discontinuous_bars_are_rejected_by_pre_close_check,
          T.test_wencai_rung_needs_date_evidence_then_unknown_truncates_chain, T.test_board_and_limit_rules,
          T.test_new_listing_first_days_are_exempt_not_limit_ups, T.test_independent_streaks_from_raw_pct_chg,
          T.test_target_day_unknown_is_not_applicable, T.test_unknown_board_is_counted_not_defaulted_to_main]


@contextmanager
def m_no_continuity():
    with mock.patch.object(ld._Walk, "_continuous", lambda self, d, part, before: (True, "", 1, 1)
                           if before is not None else (True, "", 0, 0)):
        yield


@contextmanager
def m_no_per_day_fallback():
    original = ld._Walk.__init__

    def init(self, request):
        original(self, request)
        self.ladder = [r for r in self.ladder if r == "daily_kline"]
    with mock.patch.object(ld._Walk, "__init__", init):
        yield


@contextmanager
def m_st_5pct_added():
    """An ST-style 5% width wrongly applied to one main-board symbol (the user removed ST distinctions)."""
    with mock.patch.object(ld, "BOARD_LIMIT_PCT", {**ld.BOARD_LIMIT_PCT, "MAIN": 5}):
        yield


@contextmanager
def m_gem_10pct():
    with mock.patch.object(ld, "BOARD_LIMIT_PCT", {**ld.BOARD_LIMIT_PCT, "GEM": 10, "STAR": 10}):
        yield


@contextmanager
def m_unknown_board_defaults_to_main():
    real = ld.board_of
    with mock.patch.object(ld, "board_of", lambda s: real(s) if real(s) != "UNKNOWN" else "MAIN"):
        yield


@contextmanager
def m_no_new_listing_exemption():
    with mock.patch.object(ld, "NEW_LISTING_FREE_DAYS", {"MAIN": 0, "GEM": 0, "STAR": 0, "BSE": 0}):
        yield


@contextmanager
def m_no_wencai_date_evidence():
    original = ld._Walk._pool_set

    def pool(self, d, rung):
        entry = (self.days.get(d) or {}).get(rung)
        if rung == "wencai" and isinstance(entry, dict):
            entry["date_evidence"] = d
        return original(self, d, rung)
    with mock.patch.object(ld._Walk, "_pool_set", pool):
        yield


@contextmanager
def m_invent_unknown_day():
    """A day no rung can answer is guessed to continue every streak instead of truncating the chain."""
    original = ld._Walk.resolve

    def resolve(self, d):
        ds = original(self, d)
        if ds.symbols is None and d != self.day:
            ds.symbols = frozenset(self.memo[self.day].symbols)
        return ds
    with mock.patch.object(ld._Walk, "resolve", resolve):
        yield


MUTATIONS = {"no_continuity": m_no_continuity, "no_per_day_fallback": m_no_per_day_fallback,
             "main_5pct_added": m_st_5pct_added, "gem_star_10pct": m_gem_10pct,
             "unknown_board_defaults_main": m_unknown_board_defaults_to_main,
             "no_new_listing_exemption": m_no_new_listing_exemption,
             "no_wencai_date_evidence": m_no_wencai_date_evidence, "invent_unknown_day": m_invent_unknown_day}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_is_caught(name):
    with MUTATIONS[name]():
        failed = []
        for check in CHECKS:
            try:
                check()
            except (AssertionError, Exception) as exc:   # noqa: BLE001 - any failure counts as "caught"
                failed.append(check.__name__ + ":" + type(exc).__name__)
        assert failed, f"mutation {name} survived: no ladder test failed"


def test_unmutated_checks_pass():
    for check in CHECKS:
        check()
