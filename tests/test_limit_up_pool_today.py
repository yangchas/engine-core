"""C582 tests replay only captured upstream responses and real catalog artifacts."""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine_core.data_pipeline import limit_up_today as core
from engine_core.data_pipeline.dataset_contract import check_partition, load_spec
from engine_core.data_pipeline.providers.limit_up_today_sources import SourceUnavailable
from engine_core.data_pipeline.registry import get_dataset
from engine_core.guardian import limit_up_today as runner


ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"
DAY = "2026-10-28"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _raw(name: str):
    return json.loads((REAL / name).read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _actual_names() -> dict[str, str]:
    path = REAL / "core_artifacts/c594/symbol_universe/2026-10-28/C582-actual/rows.jsonl.gz"
    return {row["symbol"]: row["name"] for row in _rows(path) if row.get("symbol") and row.get("name")}


def _replay_catalog(root: Path) -> None:
    """Point a temporary catalog at the captured same-day reference artifact."""
    manifest_path = (REAL / "core_artifacts/c594/symbol_universe/2026-10-28/C582-actual/manifest.json").resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = {"schema_version": 1, "datasets": {"symbol_universe": {
        "dates": {DAY: [{"manifest": str(manifest_path), "version": manifest["version"],
                          "available_at": manifest["available_at"]}]},
        "latest": {"trade_date": DAY, "version": manifest["version"]}}}}
    (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")


def _wencai_response() -> dict:
    records = _raw("wencai_limit_up_pool_today_2026-10-28_0702.json")
    return {"records": records, "reported_total": len(records), "request_count": 1,
            "complete": True, "truncated": False}


def test_registry_and_contract_spec_register_pit_dataset():
    assert runner.TodayDeps().publish_hot is None
    spec = get_dataset("limit_up_pool_today")
    contract = load_spec("limit_up_pool_today")
    assert spec.key_fields == ("symbol", "date", "as_of")
    assert spec.source_preference == ("redis_q2", "wencai")
    assert spec.redis_ttl_seconds == 604800
    assert contract["primary_key"] == ["symbol", "date", "as_of"]
    assert contract["columns"]["seal_amount"]["all_null_ok"] is True


def test_board_bands_and_real_q2_pit_filtering():
    assert core.normalize_symbol("000001.SZ") == "000001"
    assert [core.board_limit_percent(code) for code in
            ("688001", "689001", "300750", "830001", "430001", "920001", "600519")] == [20, 20, 20, 30, 30, 30, 10]
    snapshot = _raw("q2_limit_up_pool_today_2026-10-28_0702.json")
    built = core.build_redis_pool(
        DAY, datetime.fromisoformat("2026-10-28T15:00:01+08:00"),
        snapshot["active_symbols"], snapshot["hashes"], st_names=_actual_names())
    assert built["rows"]
    assert built["quality"]["future_quote"] > 0
    assert built["quality"]["stale_quote"] > 0
    cutoff = datetime.fromisoformat(built["as_of"])
    for row in built["rows"]:
        assert datetime.fromisoformat(row["source_ts"]) <= cutoff
        assert row["seal_amount"] is None
        assert row["st_status"] == "NON_ST"
    unavailable = core.build_redis_pool(
        DAY, datetime.fromisoformat("2026-10-28T15:00:10+08:00"),
        snapshot["active_symbols"], snapshot["hashes"], st_names=None)
    assert unavailable["rows"] and all(row["st_status"] == "UNAVAILABLE" for row in unavailable["rows"])


def test_wencai_real_dated_fields_and_three_source_close_crosscheck():
    q2 = _raw("q2_limit_up_pool_today_2026-10-28_0702.json")
    wencai_records = _raw("wencai_limit_up_pool_today_2026-10-28_0702.json")
    wencai = core.parse_wencai_pool(wencai_records, DAY)
    assert wencai["date_suffix_verified"] is True
    assert wencai["date_suffixes"] == ["20261028"]
    seal_row = next(row for row in wencai["rows"] if row["symbol"] == "000554")
    assert seal_row["seal_amount"] == pytest.approx(2_563_158.0)
    source_row = next(row for row in wencai_records if core._record_symbol(row) == "000554")
    assert float(source_row["涨停封单量[20261028]"]) * float(source_row["最新价"]) == pytest.approx(seal_row["seal_amount"])

    names = _actual_names()
    redis = core.build_redis_pool(
        DAY, datetime.fromisoformat("2026-10-28T15:00:10+08:00"),
        q2["active_symbols"], q2["hashes"], st_names=names)
    daily_path = REAL / "core_artifacts/c594/daily_kline/2026-10-28/C582-formal/rows.jsonl.gz"
    daily = core.derive_daily_kline_limit_up(_rows(daily_path), DAY, st_names=names)
    redis_set, daily_set = {row["symbol"] for row in redis["rows"]}, set(daily["symbols"])
    wencai_set = {row["symbol"] for row in wencai["rows"]}
    assert len(redis_set) == len(daily_set) == 71
    assert redis_set == daily_set
    assert sorted(redis_set - wencai_set) == ["605488"]
    assert sorted(wencai_set - redis_set) == ["920220", "920896"]
    assert "920220" not in {row["symbol"] for row in _rows(daily_path)}
    assert "920896" not in {row["symbol"] for row in _rows(daily_path)}


def test_runner_keeps_distinct_pit_versions_and_checks_contract(tmp_path):
    _replay_catalog(tmp_path)
    snapshot = _raw("q2_limit_up_pool_today_2026-10-28_0702.json")
    clock = lambda: datetime.fromisoformat("2026-10-28T15:05:00+08:00")
    deps = runner.TodayDeps(redis_snapshot=lambda _day: snapshot, clock=clock,
                            publish_hot=lambda _manifest, _rows: {"status": "offline_replay"})
    versions = []
    for as_of in ("2026-10-28T15:00:01+08:00", "2026-10-28T15:00:10+08:00"):
        code, response = runner.run({"data_date": DAY, "as_of": as_of,
                                     "args": {"tier": "redis", "failure_fallback": "none"}},
                                    tmp_path, deps=deps)
        assert code in (0, 10)
        versions.append(response["detail"]["version"])
    assert versions[0] != versions[1]
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    stored = catalog["datasets"]["limit_up_pool_today"]["dates"][DAY]
    assert len(stored) == 2
    assert {item["as_of"] for item in stored} == {
        "2026-10-28T15:00:01.000+08:00", "2026-10-28T15:00:10.000+08:00"}
    report = check_partition(tmp_path, "limit_up_pool_today", DAY, version=versions[-1],
                             spec=load_spec("limit_up_pool_today"))
    assert report.passed, report.summary()


def test_wencai_budget_is_one_request_and_redis_failure_does_not_poll(tmp_path):
    _replay_catalog(tmp_path)
    snapshot = _raw("q2_limit_up_pool_today_2026-10-28_0702.json")
    response = _wencai_response()
    calls = []
    deps = runner.TodayDeps(
        redis_snapshot=lambda _day: snapshot,
        wencai_snapshot=lambda _day: calls.append(_day) or response,
        clock=lambda: datetime.fromisoformat("2026-10-28T15:20:01+08:00"),
        publish_hot=lambda _manifest, _rows: {"status": "offline_replay"})
    request = {"data_date": DAY, "as_of": "2026-10-28T15:20:00+08:00",
               "args": {"tier": "close_confirm", "wencai_confirmation": True,
                        "wencai_min_interval_seconds": 1800, "wencai_max_requests_per_day": 1}}
    code, first = runner.run(request, tmp_path, deps=deps)
    assert code in (0, 10)
    assert first["detail"]["request_count"] == 1
    code, second = runner.run(request, tmp_path, deps=deps)
    assert code == 40 and second["error_code"] == "WENCAI_THROTTLED"
    assert calls == [DAY]

    early_calls = []
    early_deps = runner.TodayDeps(
        redis_snapshot=lambda _day: (_ for _ in ()).throw(SourceUnavailable("offline")),
        wencai_snapshot=lambda _day: early_calls.append(_day) or response,
        clock=lambda: datetime.fromisoformat("2026-10-28T15:19:59+08:00"))
    code, early = runner.run(request, tmp_path / "early-window", deps=early_deps)
    assert code == 64 and early["error_code"] == "WENCAI_WINDOW_CLOSED"
    assert early_calls == []

    offline = runner.TodayDeps(
        redis_snapshot=lambda _day: (_ for _ in ()).throw(SourceUnavailable("offline")),
        wencai_snapshot=lambda _day: pytest.fail("Redis tier must not call WenCai"), clock=deps.clock)
    code, failed = runner.run({"data_date": DAY, "as_of": "2026-10-28T15:10:00+08:00",
                               "args": {"tier": "redis", "failure_fallback": "none"}},
                              tmp_path / "offline", deps=offline)
    assert code == 75 and failed["error_code"] == "Q2_UNAVAILABLE"

    fallback_deps = runner.TodayDeps(
        redis_snapshot=lambda _day: (_ for _ in ()).throw(SourceUnavailable("offline")),
        wencai_snapshot=lambda _day: response, clock=deps.clock,
        publish_hot=lambda _manifest, _rows: {"status": "offline_replay"})
    code, fallback = runner.run(request, tmp_path / "configured-close-fallback", deps=fallback_deps)
    assert code == 0 and fallback["detail"]["business_status"] == "FORMAL"


def test_schedule_has_redis_steps_aligned_and_one_close_wencai_event():
    schedule_path = ROOT / "src/engine_core/guardian/schedule.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    native = json.loads((ROOT / "native/guardiand/testdata/schedule.json").read_text(encoding="utf-8"))
    assert schedule == native
    events = schedule["events"]
    today = [event for event in events if event.get("args", {}).get("dataset") == "limit_up_pool_today"]
    redis = [event for event in today if event["args"].get("tier") == "redis"]
    close = [event for event in today if event["args"].get("tier") == "close_confirm"]
    hot_times = {event["at"] for event in events if str(event.get("event_id", "")).startswith("kaipan_hot_")}
    assert len(redis) == 18 and {event["at"] for event in redis} == hot_times
    assert all(event["cost"] == "light" and event["max_attempts"] == 3 and
               event["retry_backoff"] == "1s" and event["args"]["failure_fallback"] == "none" for event in redis)
    assert all("wencai" not in json.dumps(event["args"]).lower() for event in redis)
    assert len(close) == 1 and close[0]["at"] == "15:20:00"
    assert close[0]["max_attempts"] == 1 and close[0]["args"]["wencai_min_interval_seconds"] >= 1800
    assert close[0]["args"]["wencai_max_requests_per_day"] == 1
