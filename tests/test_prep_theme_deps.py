"""C587: startup_0830 theme-snapshot dependencies: wait/retry inside the window, degrade once, specific reasons."""
from __future__ import annotations

import json
from pathlib import Path

import engine_core.prep_trade_day as prep
from engine_core.data_pipeline.storage import ArtifactCatalog

DAY, PREV = "2026-10-19", "2026-10-16"


def _calendar(root):
    fixture = Path(__file__).parent / "fixtures" / "trade_calendar"
    payload = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"]
    raw = {path.name: path.read_bytes() for path in fixture.glob("*.json")}
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    TradeCalendarStore(root).publish_confirmed(dates, source="C325 BaoStock+SZSE", fetched_at="2026-10-20T00:00:00+00:00",
                                               coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)


class Harness:
    """Fake theme builder: fails with the given reasons for the first ``fail_polls`` calls."""

    def __init__(self, monkeypatch, fail_polls, reasons=("limit_up_pool:limit_up_pool[2026-10-16]:available_at_after_asof(x)",),
                 ok_diag=()):
        self.calls, self.sleeps, self.jobs = 0, [], []
        self.fail_polls, self.reasons, self.ok_diag = fail_polls, list(reasons), list(ok_diag)
        monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache", self.theme)
        monkeypatch.setattr(prep, "_run", self.run)

    def theme(self, catalog, trade_date, previous, asof, diagnostics=None):
        self.calls += 1
        if self.calls <= self.fail_polls:
            diagnostics.extend(self.reasons)
            return False
        diagnostics.extend(self.ok_diag)
        return True

    def run(self, _repo, script, args):
        if script == "run_data_jobs.py" and "--job" in args:
            self.jobs.append(args[args.index("--job") + 1])
        return 0, "ok"


def report(tmp_path, h, **kw):
    return prep.build_prep_report(tmp_path, DAY, write=True, asof=None, sleep=h.sleeps.append, **kw)


def theme_errors(rep):
    return [e for e in rep["errors"] if e.startswith("theme_snapshot")]


def test_late_dependency_is_waited_for_and_refetched_not_failed(tmp_path, monkeypatch):
    _calendar(tmp_path)
    h = Harness(monkeypatch, fail_polls=2)
    rep = report(tmp_path, h, theme_wait_seconds=120, theme_poll_seconds=30, theme_on_missing="fail")
    assert h.calls == 3 and h.sleeps == [30, 30]
    assert not theme_errors(rep) and not rep["warnings"]
    assert h.jobs.count("limit_up_pool") >= 2          # the missing input is re-fetched at every poll


def test_missing_dependency_degrades_with_one_warning_after_the_window(tmp_path, monkeypatch):
    _calendar(tmp_path)
    h = Harness(monkeypatch, fail_polls=99, reasons=["limit_reasons:no_pit_ready_rows", "plate_members:absent_from_catalog"])
    rep = report(tmp_path, h, theme_wait_seconds=60, theme_poll_seconds=30, theme_on_missing="degrade")
    assert abs(sum(h.sleeps) - 60) < 0.5 and h.calls == 3
    assert not theme_errors(rep)
    assert len(rep["warnings"]) == 1
    assert "limit_reasons:no_pit_ready_rows" in rep["warnings"][0] and "plate_members:absent" in rep["warnings"][0]
    assert rep["status"] == "PARTIAL"


def test_fail_mode_names_each_missing_input(tmp_path, monkeypatch):
    _calendar(tmp_path)
    h = Harness(monkeypatch, fail_polls=99, reasons=["limit_up_pool:limit_up_pool[2026-10-16]:absent_from_catalog",
                                                      "plate_registry:no_version"])
    rep = report(tmp_path, h, theme_wait_seconds=0, theme_on_missing="fail")
    (error,) = theme_errors(rep)
    assert error.startswith("theme_snapshot:PRECOMPUTE_UNAVAILABLE:")
    assert "limit_up_pool[2026-10-16]:absent_from_catalog" in error and "plate_registry:no_version" in error
    assert h.sleeps == [] and h.calls == 1                 # window 0 -> no wait


def test_built_but_members_missing_keeps_waiting_then_warns(tmp_path, monkeypatch):
    _calendar(tmp_path)
    h = Harness(monkeypatch, fail_polls=0, ok_diag=["plate_members:plate_members[2026-10-16]:absent_from_catalog(degraded_input)"])
    rep = report(tmp_path, h, theme_wait_seconds=30, theme_poll_seconds=30, theme_on_missing="degrade")
    assert h.calls == 2 and not theme_errors(rep)
    assert rep["warnings"][0].startswith("theme_snapshot:DEGRADED_INPUT:")
    assert "plate_members" in h.jobs


def test_live_asof_is_refreshed_after_the_fetches(tmp_path, monkeypatch):
    # the 10-28 failure: inputs fetched during the run became available at 08:31, after the 08:30 cut-off taken at start
    import time
    from datetime import datetime
    _calendar(tmp_path)
    seen, ran = [], []

    def fake_run(*_a):
        time.sleep(0.02)
        ran.append(datetime.now().astimezone())
        return 0, "ok"
    monkeypatch.setattr(prep, "_run", fake_run)
    monkeypatch.setattr(prep, "_prepare_theme_snapshot_cache",
                        lambda catalog, trade_date, previous, asof, diagnostics=None: (seen.append(asof), True)[1])
    prep.build_prep_report(tmp_path, DAY, write=True, asof=None)
    assert ran and datetime.fromisoformat(seen[0]) >= ran[-1]
    # an explicit as-of (replay) is never moved
    seen.clear()
    prep.build_prep_report(tmp_path, DAY, write=True, asof="2026-10-19T08:30:00+08:00")
    assert set(seen) == {"2026-10-19T08:30:00+08:00"}


def test_why_unavailable_distinguishes_absent_late_and_failed(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    assert prep._why_unavailable(catalog, "limit_reasons", PREV, "2026-10-19T08:30:00+08:00").endswith("absent_from_catalog")
    catalog.commit("limit_reasons", PREV, [{"symbol": "000001"}],
                   {"trade_date": PREV, "available_at": "2026-10-19T08:31:00+08:00"}, source="fixture")
    why = prep._why_unavailable(catalog, "limit_reasons", PREV, "2026-10-19T08:30:00+08:00")
    assert "available_at_after_asof" in why
    catalog.commit("plate_members", PREV, [{"symbol": "000001"}], {"trade_date": PREV, "quality": {"status": "FAILED"}},
                   source="fixture")
    assert prep._why_unavailable(catalog, "plate_members", PREV, "2026-10-19T08:30:00+08:00").endswith("all_versions_failed_quality")


def test_real_theme_builder_reports_specific_missing_inputs(tmp_path):
    _calendar(tmp_path)
    catalog = ArtifactCatalog(tmp_path)
    diag: list[str] = []
    assert prep._prepare_theme_snapshot_cache(catalog, DAY, PREV, "2026-10-19T08:30:00+08:00", diagnostics=diag) is False
    joined = " | ".join(diag)
    assert "limit_up_pool:limit_up_pool[2026-10-16]:absent_from_catalog" in joined
    assert "plate_registry:" in joined and "limit_reasons:" in joined


def test_command_line_carries_schedule_policy():
    from types import SimpleNamespace
    from engine_core.guardian.commands import build_handler_command
    from engine_core.guardian.scheduler import default_events
    ev = next(e for e in default_events() if e.event_id == "startup_0830")
    cmd = build_handler_command("premarket_data", ev, SimpleNamespace(write=True), Path("/x"), DAY, "py", Path("/r"))
    assert cmd[cmd.index("--theme-wait-seconds") + 1] == "180" and cmd[cmd.index("--theme-on-missing") + 1] == "degrade"
