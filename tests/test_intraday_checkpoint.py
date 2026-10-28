"""C587: Redis q2 intraday checkpoint (pure core, gate, capture adapter) and the startup theme-dependency wait."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from engine_core.data_pipeline import intraday_checkpoint as core
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian import gates
from engine_core.guardian import intraday_checkpoint_run as run
from engine_core.guardian.scheduler import default_events

TZ = timezone(timedelta(hours=8))
DAY = "2026-10-19"
CUT = datetime(2026, 10, 19, 9, 33, 0, tzinfo=TZ)


def ms(dt):
    return str(int(dt.timestamp() * 1000))


class FakeRedis:
    def __init__(self, hashes, day="20261019", writes=None):
        self.hashes, self.day = hashes, day
        self.commands = []

    def smembers(self, key):
        self.commands.append(("smembers", key))
        return set(self.hashes) if key == "q2:active:" + self.day else set()

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return dict(self.hashes.get(key[3:], {}))

    def pipeline(self, transaction=False):
        outer = self

        class Pipe:
            def __init__(self):
                self.keys = []

            def hgetall(self, key):
                self.keys.append(key)

            def execute(self):
                return [outer.hgetall(k) for k in self.keys]
        return Pipe()


def q2(ts, px=9790):
    return {"px": str(px), "pc": "9820", "amt": "327073696", "vol": "331617", "ts": ms(ts), "mx": "9930", "mn": "9450", "ph": "2"}


def market(n=4200, ts=CUT + timedelta(seconds=7), late=0, early=0):
    out = {}
    for i in range(n):
        t = ts
        if i < early:
            t = CUT - timedelta(seconds=20)
        elif i < early + late:
            t = CUT + timedelta(seconds=300)
        out[f"{600000 + i:06d}"] = q2(t)
    return out


CFG = {"sources": ["redis_q2"], "tolerance_seconds": 60, "min_symbols": 4000}


def request(hashes, **over):
    base = {"trade_date": DAY, "cutoff": "09:33:00", "tolerance_seconds": 60, "min_symbols": 4000,
            "symbols": list(hashes), "raw": hashes, "read_at": "2026-10-19T01:35:00+00:00"}
    base.update(over)
    return base


def test_core_keeps_only_rows_inside_window_and_records_evidence():
    part = core.build_intraday_checkpoint(request(market(4200, late=100, early=50)))
    assert part.notes["state"] == "ready"
    assert len(part.rows) == 4050 and part.manifest["row_count"] == 4050
    assert part.notes["counts"]["after_window"] == 100 and part.notes["counts"]["before_window"] == 50
    cut, upper = CUT, CUT + timedelta(seconds=60)
    assert all(cut <= datetime.fromisoformat(r["ts"]) <= upper for r in part.rows)
    m = part.manifest
    assert m["source"] == "redis_q2_checkpoint" and m["business_status"] == "PROVISIONAL" and m["degraded"] is False
    assert m["as_of"] == cut.isoformat() and m["checkpoint"] == "09:33:00"
    assert m["quality"]["expected_count"] == 4200 and m["quality"]["dropped"]["after_window"] == 100
    first = part.rows[0]
    assert first["price"] == 9.79 and first["pre_close"] == 9.82 and first["volume_hands"] == 331617.0
    assert first["pct_chg"] == pytest.approx(-0.3055, abs=1e-3) and first["date"] == DAY


def test_core_rejects_wrong_date_missing_ts_and_empty_rows():
    hashes = market(4200)
    yesterday = CUT - timedelta(days=1)
    for i in range(300):
        hashes[f"{600000 + i:06d}"] = q2(yesterday)
    hashes["600300"] = {"px": "1"}
    hashes["600301"] = {}
    part = core.build_intraday_checkpoint(request(hashes))
    c = part.notes["counts"]
    assert c["other_date"] == 300 and c["no_ts"] == 1 and c["empty"] == 1
    assert len(part.rows) == 4200 - 300 - 2 and part.notes["state"] == "not_yet" and part.manifest["quality"]["status"] == "FAILED"


def test_window_state_not_yet_vs_passed():
    not_yet = core.build_intraday_checkpoint(request(market(4200, early=1000)))
    assert not_yet.notes["state"] == "not_yet"           # old ticks may still be refreshed into the window
    passed = core.build_intraday_checkpoint(request(market(4200, late=1000)))
    assert passed.notes["state"] == "passed"            # 1000 rows already overwritten by later ticks: unreachable


def test_min_symbols_threshold_is_exact():
    assert core.build_intraday_checkpoint(request(market(4000))).notes["state"] == "ready"
    assert core.build_intraday_checkpoint(request(market(3999))).notes["state"] != "ready"


def test_config_rejects_unknown_keys_and_bad_sources():
    with pytest.raises(core.CheckpointError):
        core.parse_config({"checkpoint": {"tolerence_seconds": 5}})
    with pytest.raises(core.CheckpointError):
        core.parse_config({"checkpoint": {"sources": ["ftp"]}})
    assert core.parse_config({"checkpoint": {"sources": ["td", "redis_q2"]}})["sources"] == ["td", "redis_q2"]


def test_schedule_declares_checkpoint_policy_for_all_intraday_events():
    events = {e.event_id: e for e in default_events()}
    for name in ("intraday_33", "intraday_36", "intraday_39", "intraday_42", "intraday_45"):
        cfg = core.parse_config(events[name].args)
        assert cfg["sources"] == ["redis_q2", "td"] and cfg["min_symbols"] == 4000 and cfg["tolerance_seconds"] == 60
    # open_report events keep the TD-only gate
    assert not (events["open_report_0935"].args or {}).get("checkpoint")


def event(name="intraday_33", **kw):
    return dataclasses.replace(next(e for e in default_events() if e.event_id == name), **kw)


def gate(ctx, now="2026-10-19T09:33:30+08:00", ev=None):
    gates._CACHE.clear()
    return gates.checkpoint_ready(ev or event(), datetime.fromisoformat(now), ctx)


def test_gate_ready_from_redis_after_td_deadline_race():
    # the TD window was ready only ~2:50 after the cutoff; Redis rows carry the feed's own ts, so 09:33:30 is enough
    res = gate({"q2_redis": FakeRedis(market(4200))})
    assert res["status"] == "ready" and res["source"] == "redis_q2" and res["row_count"] == 4200
    assert res["probes"][0]["state"] == "ready"


def test_gate_not_ready_below_threshold_and_window_not_reached():
    assert gate({"q2_redis": FakeRedis(market(4200, early=1500))})["reason"] == "checkpoint_below_threshold"
    early = gate({"q2_redis": FakeRedis(market(4200))}, now="2026-10-19T09:32:50+08:00")
    assert early["status"] == "not_ready" and early["reason"] == "WINDOW_NOT_REACHED"


def test_gate_reports_window_passed_without_relabeling_later_data():
    res = gate({"q2_redis": FakeRedis(market(4200, late=4200))})
    assert res["status"] == "not_ready" and res["reason"] == "CHECKPOINT_WINDOW_PASSED"


def test_gate_sampling_matches_full_read_decision():
    # > SAMPLE_SIZE symbols: a window with everything before the cutoff is rejected from the sample alone
    fake = FakeRedis(market(4200, early=4200))
    res = gate({"q2_redis": fake})
    assert res["status"] == "not_ready"
    assert sum(1 for c in fake.commands if c[0] == "hgetall") <= run.SAMPLE_SIZE + 5


def test_gate_falls_back_to_td_when_redis_unreadable_and_before_td_until(monkeypatch):
    class Broken:
        def smembers(self, key):
            raise ConnectionError("down")
    monkeypatch.setattr(gates, "_query_bounded",
                        lambda sql: ([(f"600{i:03d}", "2026-10-19 09:33:02.750000") for i in range(4000)], 1.0))
    res = gate({"q2_redis": Broken()})
    assert res["status"] == "ready" and res["source"] == "td" and res["checkpoint_ts"] == "2026-10-19 09:33:02.750000"
    assert [p["source"] for p in res["probes"]] == ["redis_q2", "td"]


def test_gate_td_is_skipped_after_td_until_and_source_order_is_configuration(monkeypatch):
    calls = []
    monkeypatch.setattr(gates, "_query_bounded", lambda sql: (calls.append(sql), ([], 1.0))[1])
    late = event("intraday_45")           # cutoff 09:45:00 <= td_until -> TD may be asked
    res = gate({"q2_redis": FakeRedis(market(10), day="20261019")}, now="2026-10-19T09:45:30+08:00", ev=late)
    assert res["status"] == "not_ready" and calls
    calls.clear()
    pm = event("intraday_33", start="10:30:05", deadline="10:36:59", args={"checkpoint": dict(CFG, sources=["redis_q2", "td"], td_until="09:45:59")})
    res = gate({"q2_redis": FakeRedis(market(10))}, now="2026-10-19T10:30:30+08:00", ev=pm)
    assert not calls and res["probes"][1]["state"] == "skipped"
    # td-first configuration really asks TD first and wins
    monkeypatch.setattr(gates, "_query_bounded",
                        lambda sql: ([(f"600{i:03d}", "2026-10-19 09:33:02") for i in range(4000)], 1.0))
    tdfirst = event("intraday_33", args={"checkpoint": dict(CFG, sources=["td", "redis_q2"], td_until="09:45:59")})
    res = gate({"q2_redis": FakeRedis(market(4200))}, ev=tdfirst)
    assert res["source"] == "td" and [p["source"] for p in res["probes"]] == ["td"]


def req(args=None, **over):
    base = {"trade_date": DAY, "data_date": DAY, "start": "09:33:05", "args": args or {"checkpoint": CFG}}
    base.update(over)
    return base


def test_capture_commits_partition_readable_back(tmp_path):
    code, reply = run.run_checkpoint(req(), tmp_path, redis=FakeRedis(market(4200, late=20)))
    assert code == 0 and reply["status"] == "ok" and reply["detail"]["row_count"] == 4180
    catalog = ArtifactCatalog(tmp_path)
    path = core.locate_checkpoint(catalog, DAY, "09:33:00")
    rows = catalog.read_rows(path)
    manifest = json.loads(path.read_text())
    assert len(rows) == 4180 and manifest["source"] == "redis_q2_checkpoint"
    assert "source=redis_q2_checkpoint" in str(path)
    assert manifest["provider_attempts"][0]["provider"] == "redis_q2" and manifest["provider_attempts"][0]["result"] == "ok"
    assert core.locate_checkpoint(catalog, DAY, "09:36:00") is None
    # same input twice -> no new version (idempotent)
    run.run_checkpoint(req(), tmp_path, redis=FakeRedis(market(4200, late=20)))
    assert len(catalog.read()["datasets"][core.DATASET]["dates"][DAY]) == 1


def test_capture_missed_window_is_failure_not_a_relabeled_snapshot(tmp_path):
    code, reply = run.run_checkpoint(req(), tmp_path, redis=FakeRedis(market(4200, late=4200)))
    assert code == 40 and reply["error_code"] == "CHECKPOINT_WINDOW_PASSED" and reply["retryable"] is False
    assert not (tmp_path / core.DATASET).exists()
    code, reply = run.run_checkpoint(req(), tmp_path, redis=FakeRedis(market(4200, early=4200)))
    assert code == 30 and reply["error_code"] == "CHECKPOINT_NOT_YET" and reply["retryable"] is True


def test_capture_unreadable_redis_is_retryable_and_td_is_not_used_after_cutoff(tmp_path):
    class Broken:
        def smembers(self, key):
            raise TimeoutError("x")
    args = {"checkpoint": dict(CFG, sources=["redis_q2", "td"])}
    code, reply = run.run_checkpoint(req(args, start="10:30:05"), tmp_path, redis=Broken())
    assert code == 30 and reply["detail"]["attempts"][0]["result"] == "SOURCE_UNREADABLE"
    assert reply["detail"]["attempts"][1]["reason"].startswith("td_has_no_ticks_after")


def test_capture_td_fallback_is_marked_degraded(tmp_path):
    class Broken:
        def smembers(self, key):
            raise TimeoutError("x")

    class Cursor:
        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [(CUT + timedelta(seconds=2), 9790, 9820, 3.2e8, 1000, 9930, 9450, f"{600000 + i:06d}") for i in range(4100)]
    class Td:
        def cursor(self):
            return Cursor()
    args = {"checkpoint": dict(CFG, sources=["redis_q2", "td"], td_until="09:45:59")}
    code, reply = run.run_checkpoint(req(args), tmp_path, redis=Broken(), td=Td())
    assert code == 10 and reply["status"] == "degraded" and reply["detail"]["source"] == "td_stock_tick_v2_checkpoint"
    manifest = json.loads(core.locate_checkpoint(ArtifactCatalog(tmp_path), DAY, "09:33:00").read_text())
    assert manifest["degraded"] is True and manifest["provider_attempts"][0]["result"] == "SOURCE_UNREADABLE"


def test_capture_dry_run_and_bad_config_do_not_touch_redis_or_disk(tmp_path):
    class Boom:
        def smembers(self, key):
            raise AssertionError("no redis in dry run")
    assert run.run_checkpoint(req(), tmp_path, dry_run=True, redis=Boom())[0] == 0
    code, reply = run.run_checkpoint(req({"checkpoint": {"bogus": 1}}), tmp_path, redis=Boom())
    assert code == 64 and reply["error_code"] == "CONFIG_ERROR"
    assert list(tmp_path.iterdir()) == []


def test_capture_only_uses_read_commands():
    fake = FakeRedis(market(50))
    core.read_q2(fake, DAY)
    assert {c[0] for c in fake.commands} <= {"smembers", "hgetall"}


# ---- handler wiring: checkpoint first, legacy hot_plates never fails the event ----------------------------------------
def test_handler_returns_checkpoint_failure_without_running_legacy(monkeypatch, tmp_path):
    from examples import guardian_handler as gh
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GUARDIAN_DRY_RUN", raising=False)
    monkeypatch.setattr(run, "run_checkpoint", lambda *a, **k: (40, {"contract": 1, "status": "failed",
                        "error_code": "CHECKPOINT_WINDOW_PASSED", "retryable": False, "detail": {}}))
    import examples.core_guardian as cg
    monkeypatch.setattr(cg, "_command_handler", lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy ran")))
    ev = event("intraday_33")
    code, reply = gh._run({"event": "intraday_33", "handler": "intraday_data", "trade_date": DAY, "asof": DAY + "T09:33:05+08:00",
                           "args": dict(ev.args)})
    assert code == 40 and reply["error_code"] == "CHECKPOINT_WINDOW_PASSED"


def test_handler_legacy_failure_degrades_but_keeps_checkpoint(monkeypatch, tmp_path):
    from examples import guardian_handler as gh
    import examples.core_guardian as cg
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GUARDIAN_DRY_RUN", raising=False)
    monkeypatch.setattr(run, "run_checkpoint", lambda *a, **k: (0, {"contract": 1, "status": "ok", "detail": {"row_count": 4100}}))

    def boom(*_a, **_k):
        def handler(*_x, **_y):
            raise RuntimeError("kaipan down")
        return handler
    monkeypatch.setattr(cg, "_command_handler", boom)
    ev = event("intraday_33")
    code, reply = gh._run({"event": "intraday_33", "handler": "intraday_data", "trade_date": DAY, "asof": DAY + "T09:33:05+08:00",
                           "args": dict(ev.args)})
    assert code == 0 and reply["status"] == "degraded"
    assert reply["detail"]["checkpoint"] == {"row_count": 4100} and "kaipan down" in reply["detail"]["hot_plates_error"]


def test_gate_bridge_uses_runtime_start_and_args(monkeypatch):
    # a shifted-trigger run must gate on its own cut-off, not on the repository's 09:33
    from examples import guardian_handler as gh
    seen = {}
    monkeypatch.setattr(gates, "default_gates", lambda: {"checkpoint_ready": lambda ev, now, ctx: (seen.update(start=ev.start, args=ev.args), {"status": "ready"})[1]})
    gh._gate("checkpoint_ready", {"event": "intraday_33", "asof": "2026-10-28T11:25:05+08:00",
                                  "args": {"checkpoint": {"min_symbols": 10}}})
    assert seen["start"] == "11:25:05" and seen["args"] == {"checkpoint": {"min_symbols": 10}}


def test_schedule_has_d1_refs_event_before_startup():
    events = {e.event_id: e for e in default_events()}
    e = events["d1_refs_0045"]
    assert e.handler == "kaipan_fetch" and e.data_date_offset == -1 and e.start < "08:30:00"
    assert set(e.args["datasets"]) == {"plate_members", "hot_plates"}
    reasons = events["limit_reasons_d1"]
    assert reasons.handler == "kaipan_fetch" and reasons.data_date_offset == -1
    assert reasons.start == "00:45:00" and reasons.deadline == "08:50:00"
    assert reasons.depends_on == ("limit_pool_d1",)
    assert set(reasons.args["datasets"]) == {"limit_reasons"}
    deps = events["startup_0830"].args["theme_deps"]
    assert deps["on_missing"] == "degrade" and deps["wait_seconds"] > 0


def test_kaipan_fetch_postcondition_follows_event_datasets(tmp_path):
    from engine_core.guardian.commands import validate_handler_postcondition
    ok, reason = validate_handler_postcondition("kaipan_fetch", "2026-10-19", tmp_path, ["limit_reasons"])
    assert not ok and reason == "limit_reasons_missing_or_empty" or reason in {"previous_trade_date_unavailable"}
