"""C571: pure derived builders, contract compliance, stale-input detection and the fake-clock trigger chain."""
from __future__ import annotations

import builtins
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from engine_core.data_pipeline import derived_daily as dd  # noqa: E402
from engine_core.data_pipeline.dataset_contract import check_partition, load_spec  # noqa: E402
from engine_core.guardian import registry as greg  # noqa: E402
import guardian_derived_trigger_check as harness  # noqa: E402

TZ = "+08:00"


@pytest.fixture(scope="module")
def fx():
    return harness.load_fixture()


def request_for(fx, day="2026-10-27", *, status_by_day=None, as_of=None, min_status="PROVISIONAL", indicators=True):
    status_by_day = status_by_day or {}
    parts = []
    for p in fx["kline"]:
        d = p["trade_date"]
        parts.append({"trade_date": d, "version": f"v-{d}", "source": "t",
                      "business_status": status_by_day.get(d, "FORMAL" if d != "2026-10-26" else "PROVISIONAL"),
                      "available_at": f"{d}T18:30:00{TZ}", "rows": p["rows"]})
    req = {"trade_date": day, "as_of": as_of or f"{day}T23:00:00{TZ}", "min_upstream_status": min_status,
           "daily_kline": parts,
           "symbol_universe": {"trade_date": day, "version": "u-" + day, "rows": fx["universe"][day]["rows"]}}
    if indicators:
        req["daily_indicators"] = {"trade_date": day, "version": "i-" + day, "business_status": "FORMAL", "source": "derived_v1"}
    return req


# ---------------------------------------------------------------- purity / decoupling
def test_pure_module_has_no_environment_disk_or_guardian_access():
    text = (ROOT / "src/engine_core/data_pipeline/derived_daily.py").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for forbidden in ("os.environ", "getenv", "import os", "open(", "write_text", "write_bytes", "datetime.now",
                      "time.time", "import guardian", "from ..guardian", "engine_core.guardian", "redis", "subprocess"):
        assert forbidden not in code, forbidden


def test_builders_do_not_write_or_read_files(fx, monkeypatch):
    request = request_for(fx)
    real_open = builtins.open

    def guarded(file, mode="r", *a, **k):
        assert "w" not in mode and "a" not in mode and "x" not in mode, f"write attempted: {file}"
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.delenv("CORE_DATA_ROOT", raising=False)
    assert dd.build_daily_indicators(request).rows
    assert dd.build_daily_report_features(request).rows


def test_same_request_gives_identical_rows_and_inputs(fx):
    a, b = dd.build_daily_indicators(request_for(fx)), dd.build_daily_indicators(request_for(fx))
    assert a.rows == b.rows and a.manifest["inputs"] == b.manifest["inputs"]


# ---------------------------------------------------------------- values
def test_indicator_values_match_independent_recomputation(fx):
    request = request_for(fx)
    history = {p["trade_date"]: p["rows"] for p in fx["kline"]}
    out = {r["symbol"]: r for r in dd.build_daily_indicators(request).rows}
    checked = 0
    for symbol in sorted(out):
        expected = harness.independent_indicators(history, "2026-10-27", symbol)
        for column, value in expected.items():
            got = out[symbol][column]
            assert (value is None) == (got is None), (symbol, column)
            if value is not None:
                assert abs(value - got) <= 0.011, (symbol, column, value, got)
        checked += 1
    assert checked >= 60


def test_short_history_is_null_not_zero(fx):
    rows = {r["symbol"]: r for r in dd.build_daily_indicators(request_for(fx)).rows}
    new_listing = rows["301718"]            # first bar 2026-10-23: 3 days of history
    for column in ("ma5", "ma10", "ma20", "bias_20", "rsi_6", "change_pct_5d", "avg_turnover_5d", "limit_up_days_5", "vol_ratio"):
        assert new_listing[column] is None, column


def test_zero_amount_zero_volume_bar_becomes_null_not_zero(fx):
    part = dd.build_daily_report_features(request_for(fx, "2026-10-26"))
    row = next(r for r in part.rows if r["symbol"] == "600621")
    assert row["prev_day_amount_yuan"] is None
    assert part.manifest["quality"]["no_trade_amounts_nullified"] >= 4


def test_outside_universe_rows_are_dropped_and_counted(fx):
    part = dd.build_daily_report_features(request_for(fx, "2026-10-26"))
    symbols = {r["symbol"] for r in part.rows}
    assert not symbols & {"000016", "601059", "601198"}
    assert part.manifest["quality"]["dropped_outside_universe"] == 3
    assert dd.SENTINEL_SYMBOL in symbols


def test_pit_partition_available_after_as_of_is_invisible(fx):
    request = request_for(fx, as_of="2026-10-27T18:00:00+08:00")     # before 10-27 18:30 availability
    with pytest.raises(dd.UpstreamNotReady) as err:
        dd.build_daily_indicators(request)
    assert err.value.code == "DAILY_KLINE_MISSING"
    request = request_for(fx, "2026-10-26", as_of="2026-10-26T18:31:00+08:00")
    part = dd.build_daily_indicators(request)
    assert part.manifest["inputs"]["daily_kline"]["history_days"] == 81   # 10-27 never leaks into 10-26


def test_quality_inheritance_and_minimum(fx):
    prov = dd.build_daily_indicators(request_for(fx, "2026-10-26"))
    assert prov.manifest["business_status"] == "PROVISIONAL" and prov.manifest["degraded"] is False
    formal = dd.build_daily_indicators(request_for(fx, "2026-10-27"))
    assert formal.manifest["business_status"] == "FORMAL"
    assert formal.manifest["quality"]["history_non_formal_days"] == ["2026-10-26"]
    with pytest.raises(dd.UpstreamNotReady) as err:
        dd.build_daily_indicators(request_for(fx, "2026-10-26", min_status="FORMAL"))
    assert err.value.code == "UPSTREAM_BELOW_MIN_QUALITY"
    features = dd.build_daily_report_features({**request_for(fx, "2026-10-27"),
                                               "daily_indicators": {"trade_date": "2026-10-27", "version": "i",
                                                                    "business_status": "PROVISIONAL"}})
    assert features.manifest["business_status"] == "PROVISIONAL"   # lowest of its same-day inputs
    with pytest.raises(dd.UpstreamNotReady):
        dd.build_daily_report_features(request_for(fx, indicators=False))


def test_window_length_is_enforced(fx):
    with pytest.raises(ValueError):
        dd.build_daily_indicators({**request_for(fx), "window": {"max_days": 10}})
    short = dd.build_daily_indicators({**request_for(fx), "window": {"max_days": 20}})
    full = dd.build_daily_indicators(request_for(fx))
    assert short.manifest["inputs"]["daily_kline"]["history_days"] == 20
    assert full.manifest["inputs"]["daily_kline"]["history_days"] == 82
    assert short.manifest["inputs"]["daily_kline"]["history_digest"] != full.manifest["inputs"]["daily_kline"]["history_digest"]


# ---------------------------------------------------------------- contract on committed partitions
@pytest.fixture(scope="module")
def committed(tmp_path_factory, fx):
    run = harness.Run(tmp_path_factory.mktemp("c571"), fx)
    for day in run.days:
        if fx["universe"].get(day):
            run.land_universe(day)
    for day in run.days:
        status = "PROVISIONAL" if day == "2026-10-26" else "FORMAL"
        run.land_kline(day, status, "legacy_q2_final" if status == "PROVISIONAL" else "baostock", f"{day}T18:30:00{TZ}")
    now = "2026-10-27T21:00:00+08:00"
    run.execute(run.plan(now, "2026-10-27"), now)
    run.execute(run.plan(now, "2026-10-27"), now)
    return run


@pytest.mark.parametrize("dataset", ["daily_indicators", "daily_report_features"])
def test_committed_partition_passes_contract(committed, dataset):
    report = check_partition(committed.root, dataset, "2026-10-27")
    assert report.passed, report.summary()


def _mutated_report(committed, tmp_path, dataset, edit_manifest=None, edit_rows=None):
    import shutil
    root = tmp_path / f"copy{len(list(tmp_path.iterdir()))}"
    shutil.copytree(committed.root, root, symlinks=True)
    text = (root / "catalog.json").read_text().replace(str(committed.root), str(root))
    (root / "catalog.json").write_text(text)
    cat = json.loads(text)
    entry = cat["datasets"][dataset]["dates"]["2026-10-27"][-1]
    mpath = Path(entry["manifest"])
    manifest = json.loads(mpath.read_text().replace(str(committed.root), str(root)))
    if edit_manifest:
        edit_manifest(manifest)
    mpath.write_text(json.dumps(manifest))
    if edit_rows:
        import gzip
        rpath = Path(manifest["rows_uri"])
        rows = [json.loads(l) for l in gzip.open(rpath, "rt")]
        edit_rows(rows)
        rpath.write_bytes(gzip.compress(("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                                                   for r in rows) + "\n").encode()))
    return check_partition(root, dataset, "2026-10-27")


def _failed(report):
    return {c["id"] for c in report.checks if c["status"] == "FAIL"}


def test_contract_mutations_are_caught(committed, tmp_path):
    assert "A11" in _failed(_mutated_report(committed, tmp_path, "daily_indicators",
                                            lambda m: m["field_units"].update(ma5="cent")))
    assert "A3d" in _failed(_mutated_report(committed, tmp_path, "daily_indicators",
                                            lambda m: m.update(provider_chain=["baostock"])))
    assert "A3" in _failed(_mutated_report(committed, tmp_path, "daily_indicators", lambda m: m.pop("fetched_at")))
    assert "A5" in _failed(_mutated_report(committed, tmp_path, "daily_report_features",
                                           lambda m: m.update(business_status="GOOD")))
    assert "B4" in _failed(_mutated_report(committed, tmp_path, "daily_indicators", None,
                                           lambda rows: rows[0].update(stray_column=1)))
    assert "B1" in _failed(_mutated_report(committed, tmp_path, "daily_report_features", None,
                                           lambda rows: rows[0].update(symbol="ABC123")))
    # the sentinel exception is bounded: a second __MARKET__ row is rejected
    assert "B1a" in _failed(_mutated_report(committed, tmp_path, "daily_report_features", None,
                                            lambda rows: rows.append(copy.deepcopy(next(r for r in rows if r["symbol"] == "__MARKET__")))))


# ---------------------------------------------------------------- stale-input detection (registry)
def test_legacy_manifest_without_refs_is_compared_on_its_single_input_version():
    catalog = {"datasets": {"daily_kline": {"dates": {"2026-10-26": [
        {"version": "v1", "business_status": "PROVISIONAL"}, {"version": "v2", "business_status": "FORMAL"}]}}}}
    legacy = {"inputs": {"daily_kline": {"version": "v1"}}}
    assert dd.inputs_stale(catalog, "daily_indicators", "2026-10-26", legacy) is True
    assert dd.inputs_stale(catalog, "daily_indicators", "2026-10-26", {"inputs": {"daily_kline": {"version": "v2"}}}) is False
    assert dd.inputs_stale(catalog, "daily_indicators", "2026-10-26", {}) is False


def test_new_history_partition_makes_recorded_window_stale():
    catalog = {"datasets": {"daily_kline": {"dates": {d: [{"version": "v" + d, "business_status": "FORMAL"}]
                                                       for d in ("2026-10-23", "2026-10-26", "2026-10-27")}}}}
    manifest = {"window": {"max_days": 120}, "input_refs": [
        {"dataset": "daily_kline", "trade_date": "2026-10-23", "version": "v2026-10-23"},
        {"dataset": "daily_kline", "trade_date": "2026-10-27", "version": "v2026-10-27"}]}
    assert dd.inputs_stale(catalog, "daily_indicators", "2026-10-27", manifest) is True   # 10-26 appeared later


# ---------------------------------------------------------------- fake-clock trigger chain
def test_fake_clock_trigger_chain_passes():
    checks = harness.run_all()
    assert len(checks) >= 18 and all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


@pytest.mark.parametrize("name", ["no_input_refs", "fixed_clock_trigger", "bad_window"])
def test_fake_clock_trigger_chain_mutations_fail(name):
    checks = harness.run_all(name)
    assert any(not c["ok"] for c in checks), name


def test_fetch_item_verifies_after_the_job_not_before(tmp_path, monkeypatch):
    """A partition committed by the job itself is available at its commit instant, later than the pre-job clock."""
    from datetime import timedelta
    from engine_core.guardian import startup
    seen = {}
    monkeypatch.setattr(startup, "_run_fetch_job", lambda item, day, timeout, root, **kw: {"ok": True})
    monkeypatch.setattr(startup, "_verify_partition", lambda item, day, root, now=None: seen.update(now=now))
    before = datetime.now().astimezone()
    result = startup.fetch_item("daily_indicators", "2026-10-27", tmp_path, dry_run=False)
    assert result["status"] == "ok"
    assert seen["now"] >= before and seen["now"] - before < timedelta(seconds=5)
    startup.fetch_item("daily_indicators", "2026-10-27", tmp_path, dry_run=False, now="2026-10-27T19:00:00+08:00")
    assert seen["now"] == datetime.fromisoformat("2026-10-27T19:00:00+08:00")   # an explicit fake clock is honoured
