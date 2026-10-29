import json
import sys
from pathlib import Path
from time import strftime as real_strftime
from types import SimpleNamespace

import pytest

from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.providers import kaipan_provider
from examples import capture_kaipan_plates


ROOT = Path(__file__).resolve().parents[1]


def test_hot_plates_capture_commits_a_new_fixture_version_each_run(tmp_path, monkeypatch, capsys):
    response = json.loads((ROOT / "tests/fixtures/real/kaipan_hot_plates_2026-10-23_1441.json")
                          .read_text(encoding="utf-8"))

    class FixtureSdk:
        def getHisPlates(self, date, index, size):
            assert (date, index, size) == ("", "0", "50")
            return response

    class MemoryRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, ex=None):
            self.values[key] = value

        def delete(self, key):
            self.values.pop(key, None)

        def close(self):
            pass

    client = MemoryRedis()
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", FixtureSdk)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(
        Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client)))
    timestamps = iter(("2026-10-23T14:41:16+0800", "2026-10-23T14:41:17+0800"))
    def captured_at(format, *args):
        if format == "%Y-%m-%dT%H:%M:%S%z":
            return next(timestamps)
        return real_strftime(format, *args)
    monkeypatch.setattr(kaipan_provider.time, "strftime", captured_at)
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--max-pages", "1", "--execute"]

    now_fn = lambda: __import__("datetime").datetime.fromisoformat(
        "2026-10-23T14:41:15+08:00")
    assert capture_kaipan_plates.main(argv, now_fn=now_fn) == 0
    first_output = capsys.readouterr().out
    assert "requests=1" in first_output
    after_first = ArtifactCatalog(tmp_path).read()["datasets"]["hot_plates"]["dates"]["2026-10-23"]
    assert len(after_first) == 1

    assert capture_kaipan_plates.main(argv, now_fn=now_fn) == 0
    capsys.readouterr()
    versions = ArtifactCatalog(tmp_path).read()["datasets"]["hot_plates"]["dates"]["2026-10-23"]
    assert len(versions) == 2
    manifests = [json.loads(Path(item["manifest"]).read_text(encoding="utf-8")) for item in versions]
    assert len({manifest["fetched_at"] for manifest in manifests}) == 2
    assert all(manifest["requested_api_date"] == "" for manifest in manifests)
    assert all(manifest["event_id"].startswith("kaipan_hot_manual_") for manifest in manifests)


def test_hot_plates_capture_survives_guarded_redis_publish(tmp_path, monkeypatch, capsys):
    response = json.loads((ROOT / "tests/fixtures/real/kaipan_hot_plates_2026-10-23_1441.json")
                          .read_text(encoding="utf-8"))

    class FixtureSdk:
        def getHisPlates(self, date, index, size):
            assert (date, index, size) == ("", "0", "50")
            return response

    class GuardedRedis:
        def __init__(self):
            self.attempted_keys = []

        def get(self, _key):
            return None

        def set(self, key, _value, ex=None):
            self.attempted_keys.append(key)
            raise PermissionError("isolated_guard_rejected_write")

        def delete(self, _key):
            raise AssertionError("a rejected first write must not need rollback")

        def close(self):
            pass

    client = GuardedRedis()
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", FixtureSdk)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(
        Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client)))
    monkeypatch.setattr(kaipan_provider.time, "strftime",
                        lambda fmt, *args: "2026-10-23T14:41:16+0800"
                        if fmt == "%Y-%m-%dT%H:%M:%S%z" else real_strftime(fmt, *args))

    result = capture_kaipan_plates.main(
        ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
         "--artifact-root", str(tmp_path), "--max-pages", "1", "--execute"],
        now_fn=lambda: __import__("datetime").datetime.fromisoformat(
            "2026-10-23T14:41:15+08:00"))

    output = capsys.readouterr().out
    versions = ArtifactCatalog(tmp_path).read()["datasets"]["hot_plates"]["dates"]["2026-10-23"]
    manifest = json.loads(Path(versions[-1]["manifest"]).read_text(encoding="utf-8"))
    ledger = json.loads((tmp_path / "job_state.json").read_text(encoding="utf-8"))
    assert result == 0
    assert len(versions) == 1 and manifest["row_count"] == len(response["list"])
    assert ledger["status"]["hot_plates"]["cache_publish"] == "WARN"
    assert ledger["status"]["hot_plates"]["cache_publish_warning"] == "redis_publish_failed:PermissionError"
    assert client.attempted_keys and all(key.startswith("core:") for key in client.attempted_keys)
    assert "WARN dataset=hot_plates cache_publish=redis_publish_failed:PermissionError" in output
    assert "CAPTURED dataset=hot_plates date=2026-10-23" in output and "cache=WARN" in output


def test_late_hot_capture_startup_records_gap_without_provider_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk",
                        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")))
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--write-artifact", "--event-id", "kaipan_hot_091610",
            "--scheduled-at", "09:16:10", "--deadline", "09:17:30"]

    result = capture_kaipan_plates.main(argv, now_fn=lambda: __import__("datetime").datetime.fromisoformat(
        "2026-10-23T09:17:31+08:00"))

    assert result == 0
    assert "late_policy=gap_only" in capsys.readouterr().out
    path = ArtifactCatalog(tmp_path).locate("hot_plates_gaps", "2026-10-23")
    rows = ArtifactCatalog(tmp_path).read_rows(path)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "kaipan_hot_091610"


def test_expired_historical_hot_plates_is_gap_only_without_provider_call(
        tmp_path, monkeypatch, capsys):
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    monkeypatch.setattr(TradeCalendarStore, "all_trading_dates", lambda _self: [
        "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
        "2026-10-26", "2026-10-27", "2026-10-28",
    ])
    class ExpiredSdk:
        calls = 0

        def getHisPlates(self, *_args):
            self.calls += 1
            raise AssertionError("expired date must not be fetched")

    sdk = ExpiredSdk()
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", lambda: sdk)
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--write-artifact"]

    result = capture_kaipan_plates.main(argv, now_fn=lambda: __import__("datetime").datetime.fromisoformat(
        "2026-10-29T08:12:00+08:00"))

    assert result == 0
    output = capsys.readouterr().out
    assert "reason=historical_outside_retention" in output
    assert "requests=0" in output
    assert sdk.calls == 0
    path = ArtifactCatalog(tmp_path).locate("hot_plates_gaps", "2026-10-23")
    rows = ArtifactCatalog(tmp_path).read_rows(path)
    assert len(rows) == 1
    assert rows[0]["late_policy"] == "gap_only"
    assert rows[0]["fallback_status"] == "MISSING"


def test_transient_hot_capture_failure_returns_for_guardian_retry(tmp_path, monkeypatch):
    class FailingSdk:
        def getHisPlates(self, date, index, size):
            raise TimeoutError("temporary_timeout")

    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", FailingSdk)
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--write-artifact", "--event-id", "kaipan_hot_091610",
            "--scheduled-at", "09:16:10", "--deadline", "09:17:30"]
    now = lambda: __import__("datetime").datetime.fromisoformat("2026-10-23T09:16:11+08:00")

    with pytest.raises(RuntimeError, match="hot_plates_retryable_provider_failure"):
        capture_kaipan_plates.main(argv, now_fn=now)

    assert ArtifactCatalog(tmp_path).locate("hot_plates_gaps", "2026-10-23") is None
