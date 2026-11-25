import json
import sys
from pathlib import Path
from time import strftime as real_strftime
from types import SimpleNamespace

import pytest
from engine_core.errors import declared_result

from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.providers import kaipan_provider
from examples import capture_kaipan_plates


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_gap_redis(monkeypatch):
    from fnmatch import fnmatch
    from engine_core.data_pipeline import limit_pool_hot_view, redis_day_view
    class Redis:
        def __init__(self):
            self.values = {}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, ex=None, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True
        def scan_iter(self, match, count=100):
            return [key for key in self.values if fnmatch(key, match)]
    redis = Redis()
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: redis)
    monkeypatch.setenv('CORE_DATA_ROOT', str(Path.home() / 'core-data'))
    from engine_core.data_pipeline import jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 1792764000.0)
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 4070908800)
    return redis


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
    prefix = "core:cache:data:hot_plates:2026-10-23"
    first_pointer = json.loads(client.values[prefix])
    first = json.loads(client.values[f"{prefix}:{first_pointer['version']}"])
    assert len(first["rows"]) == len(response["list"])

    assert capture_kaipan_plates.main(argv, now_fn=now_fn) == 0
    capsys.readouterr()
    second_pointer = json.loads(client.values[prefix])
    second = json.loads(client.values[f"{prefix}:{second_pointer['version']}"])
    assert first_pointer["version"] != second_pointer["version"]
    assert first["fetched_at"] != second["fetched_at"]
    assert all(view["requested_api_date"] == "" for view in (first, second))
    assert all(view["event_id"].startswith("kaipan_hot_manual_") for view in (first, second))
    assert not (tmp_path / "catalog.json").exists()


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

    from engine_core.data_pipeline import jobs
    from engine_core.data_pipeline.jobs import JobState
    job_state = JobState()                                    # in-process state shared with the runner the CLI builds
    monkeypatch.setattr(jobs, "default_job_state", lambda: job_state)
    result = capture_kaipan_plates.main(
        ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
         "--artifact-root", str(tmp_path), "--max-pages", "1", "--execute"],
        now_fn=lambda: __import__("datetime").datetime.fromisoformat(
            "2026-10-23T14:41:15+08:00"))

    output = capsys.readouterr().out
    declared = declared_result(output)
    assert result != 0 and declared["status"] == "failed"
    assert "PermissionError" in declared["detail"]["error"]
    assert client.attempted_keys and all(key.startswith("core:") for key in client.attempted_keys)
    assert not (tmp_path / "catalog.json").exists()
    assert job_state.status("hot_plates") is None


def test_late_hot_capture_startup_records_gap_without_provider_call(tmp_path, monkeypatch, capsys, isolated_gap_redis):
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk",
                        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")))
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--write-artifact", "--event-id", "kaipan_hot_091610",
            "--scheduled-at", "09:16:10", "--deadline", "09:17:30"]

    result = capture_kaipan_plates.main(argv, now_fn=lambda: __import__("datetime").datetime.fromisoformat(
        "2026-10-23T09:17:31+08:00"))

    assert result == 0
    assert "late_policy=gap_only" in capsys.readouterr().out
    from engine_core.data_pipeline.hot_plates_gaps import _existing_gap_rows
    rows = _existing_gap_rows(tmp_path, "2026-10-23")
    assert len(rows) == 1
    assert rows[0]["event_id"] == "kaipan_hot_091610"


def test_expired_historical_hot_plates_is_gap_only_without_provider_call(
        tmp_path, monkeypatch, capsys, isolated_gap_redis):
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
    from engine_core.data_pipeline.hot_plates_gaps import _existing_gap_rows
    rows = _existing_gap_rows(tmp_path, "2026-10-23")
    assert len(rows) == 1
    assert rows[0]["late_policy"] == "gap_only"
    assert rows[0]["fallback_status"] == "MISSING"


def test_transient_hot_capture_failure_returns_for_guardian_retry(tmp_path, monkeypatch, capsys):
    class FailingSdk:
        def getHisPlates(self, date, index, size):
            raise TimeoutError("temporary_timeout")

    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", FailingSdk)
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--write-artifact", "--event-id", "kaipan_hot_091610",
            "--scheduled-at", "09:16:10", "--deadline", "09:17:30"]
    now = lambda: __import__("datetime").datetime.fromisoformat("2026-10-23T09:16:11+08:00")

    code = capture_kaipan_plates.main(argv, now_fn=now)          # the command answers with a contract line, it does not crash
    declared = declared_result(capsys.readouterr().out)
    assert code == 30 and declared["error_code"] == "SOURCE_UNREADABLE" and declared["retryable"] is True
    assert "hot_plates_retryable_provider_failure" in declared["detail"]["error"]

    from engine_core.data_pipeline.hot_plates_gaps import _existing_gap_rows
    assert _existing_gap_rows(tmp_path, "2026-10-23") == []
