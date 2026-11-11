"""C635 pre-open self-check: yesterday's limit-up pool, hot plates and daily bars, all driven by schedule args."""
import json
from pathlib import Path

import pytest

from engine_core.guardian import preopen_check
from engine_core.errors import CoreError

CHECK_DAY, DAY = "2026-10-27", "2026-10-26"


class StubCatalog:
    def __init__(self, root, datasets):
        self.root, self._datasets = root, {}
        for name, manifests in datasets.items():
            versions = []
            for index, manifest in enumerate(manifests):
                path = root / f"{name}-{index}.json"
                path.write_text(json.dumps(manifest))
                versions.append({"manifest": str(path)})
            self._datasets[name] = {"dates": {DAY: versions}}

    def read(self):
        return {"datasets": self._datasets}


def manifest(status="FORMAL", rows=47, available="2026-10-27T08:32:05.697525+08:00"):
    return {"business_status": status, "row_count": rows, "available_at": available}


CHECKS = {"limit_up_pool": {"statuses": ["FORMAL", "READY"], "min_rows": 1},
          "hot_plates": {"statuses": ["READY", "FORMAL", "PROVISIONAL"], "min_rows": 1,
                         "captured_after": "08:15:00"},
          "daily_kline": {"coverage_threshold": 0.995, "total_timeout_seconds": 1200}}
HISTORY_DONE = "2026-10-27T08:15:30+08:00"


@pytest.fixture(autouse=True)
def previous_day(monkeypatch):
    import engine_core.prep_trade_day as prep
    from engine_core.data_pipeline import limit_pool_hot_view as hot

    class Redis:
        value = None

        def get(self, key):
            assert key == f"core:view:limit_up_pool:{DAY}"
            return self.value

    monkeypatch.setattr(prep, "previous_trade_date", lambda _root, _day: DAY)
    monkeypatch.setattr(hot, "_client", lambda: Redis())
    monkeypatch.setattr(hot, "_td_store", lambda: (_ for _ in ()).throw(RuntimeError("no TD")))


def run(tmp_path, datasets, fill, checks=CHECKS, dry_run=False):
    from unittest.mock import patch
    from ladder_support import fixture
    from engine_core.data_pipeline import limit_pool_hot_view as hot

    pool = fixture()["pools"][DAY]
    selected = datasets.get("limit_up_pool", [])
    client = hot._client()
    if selected:
        selected_meta = selected[0]
        rows = [{**row, "date": DAY} for row in pool["rows"][:selected_meta["row_count"]]]
        status = selected_meta["business_status"]
        meta = {**pool, **selected_meta, "version": f"fixture-subset-{len(rows)}-{pool['version']}",
                "source": "kaipan_getHisBans", "provider": "kaipan_getHisBans",
                "row_count": len(rows), "degraded": status == "DEGRADED",
                "quality": {"status": "DEGRADED" if status == "DEGRADED" else "READY", "partial": False}}
        client.value = json.dumps(hot.envelope(DAY, rows, meta))
    request = {"trade_date": CHECK_DAY, "now": "2026-10-27T08:35:00+08:00", "args": {"checks": checks}}
    with patch.object(hot, "_client", return_value=client):
        return preopen_check.run(request, tmp_path, dry_run=dry_run, fill=fill,
                                 catalog=StubCatalog(tmp_path, {k: v for k, v in datasets.items() if k != "limit_up_pool"}))


def complete_fill(_root, day, *, args=None):
    return {"status": "NO_GAPS", "coverage": {day: 1.0}}


def healthy():
    return {"limit_up_pool": [manifest("FORMAL", 47)],
            "hot_plates": [manifest("PROVISIONAL", 241, "2026-10-26T15:10:07+08:00"),
                           manifest("PROVISIONAL", 241, HISTORY_DONE)]}


def test_everything_present_is_ok_and_names_the_checked_day(tmp_path):
    code, payload = run(tmp_path, healthy(), complete_fill)
    assert (code, payload["status"]) == (0, "ok")
    assert payload["detail"]["checked_day"] == DAY and [r["status"] for r in payload["detail"]["results"]] == ["OK"] * 3
    assert payload["detail"]["results"][0]["detail"]["storage_source"] == "redis"
    assert payload["detail"]["results"][0]["detail"]["version"] == "fixture-subset-47-sha256-396734dca9e888b2"


def test_a_missing_limit_up_pool_fails_retryably_and_names_it(tmp_path):
    datasets = healthy()
    datasets.pop("limit_up_pool")
    code, payload = run(tmp_path, datasets, complete_fill)
    assert (code, payload["error_code"], payload["retryable"]) == (75, "PREOPEN_CHECK_FAILED", True)
    assert payload["detail"]["failed"] == ["limit_up_pool"]
    datasets = healthy()
    datasets["limit_up_pool"][0]["available_at"] = "2026-10-27T08:36:00+08:00"
    code, payload = run(tmp_path, datasets, complete_fill)
    assert code == 75 and payload["detail"]["failed"] == ["limit_up_pool"]


def test_hot_plates_without_the_morning_history_fetch_are_not_ready(tmp_path):
    datasets = healthy()
    datasets["hot_plates"] = [manifest("PROVISIONAL", 241, "2026-10-26T15:10:07+08:00")]   # only the intraday shots
    code, payload = run(tmp_path, datasets, complete_fill)
    assert code == 75 and payload["detail"]["failed"] == ["hot_plates"]
    assert payload["detail"]["results"][1]["detail"]["need"]["captured_after"] == "08:15:00"


def test_a_status_the_schedule_does_not_accept_fails_even_with_rows(tmp_path):
    datasets = healthy()
    datasets["limit_up_pool"] = [manifest("DEGRADED", 47)]
    assert run(tmp_path, datasets, complete_fill)[0] == 75


def test_min_rows_comes_from_the_schedule(tmp_path):
    datasets = healthy()
    checks = {**CHECKS, "limit_up_pool": {"statuses": ["FORMAL"], "min_rows": 100}}
    assert run(tmp_path, datasets, complete_fill, checks)[0] == 75
    assert run(tmp_path, datasets, complete_fill, {**CHECKS, "limit_up_pool": {"statuses": ["FORMAL"], "min_rows": 47}})[0] == 0


@pytest.mark.parametrize("outcome,ok", [({"status": "NO_GAPS"}, True), ({"status": "COMPLETE", "written_symbols": 3}, True),
                                        ({"status": "BUSY"}, False), (CoreError("BAOSTOCK_GAP_UNRESOLVED", "x"), False)])
def test_daily_kline_check_is_the_fill_itself(tmp_path, outcome, ok):
    seen = {}

    def fill(_root, day, *, args=None):
        seen.update(day=day, args=args)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    code, payload = run(tmp_path, healthy(), fill)
    assert (code == 0) is ok
    assert seen["day"] == DAY                                              # yesterday, not today
    assert seen["args"]["daily_kline_policy"]["formal_coverage_threshold"] == 0.995
    assert seen["args"]["total_timeout_seconds"] == 1200
    if not ok:
        assert payload["detail"]["failed"] == ["daily_kline"]


def test_dry_run_never_fills(tmp_path):
    def fill(*_a, **_k):
        raise AssertionError("dry run must not fetch")

    assert run(tmp_path, healthy(), fill, dry_run=True)[0] == 0


def test_no_checks_configured_is_a_config_error_not_a_pass(tmp_path):
    code, payload = preopen_check.run({"trade_date": CHECK_DAY, "args": {}}, tmp_path)
    assert (code, payload["error_code"], payload["retryable"]) == (64, "CONFIG_ERROR", False)


def test_the_scheduled_event_reaches_the_check_through_the_boot_data_health_handler(monkeypatch):
    from examples import guardian_handler

    seen = {}
    monkeypatch.setattr(preopen_check, "run", lambda request, root, dry_run=False, **_k:
                        seen.update(request=request) or (0, {"contract": 1, "status": "ok", "error_code": "",
                                                              "retryable": False, "artifacts": [], "detail": {}}))
    schedule = json.loads((Path(__file__).resolve().parents[1] /
                           "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))
    event = next(e for e in schedule["events"] if e["event_id"] == "preopen_check_0835")
    request = {"contract": 1, "event": event["event_id"], "instance": event["event_id"], "handler": "boot_data_health",
               "trade_date": CHECK_DAY, "data_date": CHECK_DAY, "args": event["args"]}
    code, payload = guardian_handler._run(request)
    assert (code, payload["status"]) == (0, "ok")
    assert seen["request"]["trade_date"] == CHECK_DAY and set(seen["request"]["args"]["checks"]) == set(CHECKS)


def test_schedule_event_runs_before_the_heavy_cutoff_after_every_producer_it_checks():
    schedule = json.loads((Path(__file__).resolve().parents[1] /
                           "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))
    events = {e["event_id"]: e for e in schedule["events"]}
    check = events["preopen_check_0835"]
    assert check["handler"] == "boot_data_health" and check["calendar"] == "trading_day"
    assert events["hot_plates_history_0815"]["deadline"] <= check["at"]          # the history fetch is finished
    assert check["late_until"] <= schedule["global"]["heavy_cutoffs"]["PRE_MARKET"] <= "09:10:00"
    assert set(check["args"]["checks"]) == {"limit_up_pool", "hot_plates", "daily_kline"}
    for key in ("fill_max_consecutive_failures", "fill_max_failure_ratio", "lease_ttl_seconds"):
        assert key in check["args"]


def test_the_td_health_probe_is_given_more_than_the_default_five_seconds(monkeypatch):
    from engine_core.guardian import startup

    seen = {}

    def fake(fn, timeout=5.0):
        seen.setdefault(fn.__name__, timeout)
        return {"connection": "ok"}

    monkeypatch.setattr(startup, "_timed_probe", fake)
    startup._health_probes(Path("."))
    assert seen["_td_probes"] >= 15.0
