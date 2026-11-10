# 逻辑测试:验证顺序与分支,不代表真实 Redis 行为。
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine_core.calendar import CalendarCoverageError
from engine_core.data_pipeline.redis_day_view import expire_at, meta_key, view_key, write_day_view


@pytest.fixture(autouse=True)
def real_calendar(tmp_path, monkeypatch):
    root = tmp_path / "calendar-data"
    (root / "trade_calendar").mkdir(parents=True)
    shutil.copyfile("/home/exedev/core-data/trade_calendar/calendar.json", root / "trade_calendar/calendar.json")
    monkeypatch.setenv("CORE_DATA_ROOT", str(root))


class RecordingPipeline:
    def __init__(self, redis):
        self.calls = []
        self.redis = redis
        self.pending = []

    def hset(self, *args, **kwargs):
        self.calls.append(("hset", args, kwargs))
        self.pending.append(("hset", args, kwargs))
        return self

    def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))
        return self

    def expireat(self, *args):
        self.calls.append(("expireat", args, {}))
        return self

    def execute(self):
        self.calls.append(("execute", (), {}))
        for name, args, kwargs in self.pending:
            if name == "hset":
                self.redis.hashes.setdefault(args[0], {}).update(kwargs["mapping"])
        self.pending.clear()


class RecordingRedis:
    def __init__(self):
        self.pipelines = []
        self.hashes = {}

    def pipeline(self):
        pipe = RecordingPipeline(self)
        self.pipelines.append(pipe)
        return pipe

    def delete(self, key):
        self.hashes.pop(key, None)

    def hlen(self, key):
        return len(self.hashes.get(key, {}))


def real_rows():
    path = Path(__file__).parent / "fixtures/real/redis_view_stock_extra_2026-11-09.json"
    payload = json.loads(path.read_text())
    return {key: json.loads(value) for key, value in list(payload.items())[:2]}


def test_expire_at_real_calendar():
    assert expire_at("2026-11-09") == int(datetime(2026, 11, 16, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def test_write_metadata_and_expiration():
    redis = RecordingRedis()
    write_day_view(redis, "stock_extra", "2026-11-09", real_rows(), source="baostock", version="v1", available_at="2026-10-28T23:01:00+08:00", fields=["symbol"])
    calls = redis.pipelines[0].calls
    meta = json.loads(next(args[1] for name, args, _ in calls if name == "set"))
    assert meta["status"] == "COMPLETE" and meta["row_count"] == 2
    assert [(args[0], args[1]) for name, args, _ in calls if name == "expireat"] == [
        (view_key("stock_extra", "2026-11-09"), expire_at("2026-11-09")),
    ]
    assert next(args[0] for name, args, _ in calls if name == "set") == meta_key("stock_extra", "2026-11-09")
    assert next(kwargs["exat"] for name, _, kwargs in calls if name == "set") == expire_at("2026-11-09")
    assert len(redis.pipelines) == 1 and calls[-1][0] == "execute"


def test_fields_filter_and_empty_status():
    redis = RecordingRedis()
    rows = real_rows()
    write_day_view(redis, "stock_extra", "2026-11-09", rows, source="baostock", version="v1", available_at="2026-10-28T23:01:00+08:00", fields=["symbol"])
    mapping = next(kwargs["mapping"] for name, _, kwargs in redis.pipelines[0].calls if name == "hset")
    assert mapping == {key: json.dumps({"symbol": row["symbol"]}, sort_keys=True, separators=(",", ":")) for key, row in rows.items()}
    write_day_view(redis, "stock_extra", "2026-11-09", {}, source="baostock", version="v1", available_at="2026-10-28T23:01:00+08:00", fields=["symbol"])
    calls = redis.pipelines[1].calls
    assert not any(name == "hset" for name, _, _ in calls)
    assert json.loads(next(args[1] for name, args, _ in calls if name == "set"))["status"] == "EMPTY"


def test_insufficient_calendar_never_opens_pipeline():
    redis = RecordingRedis()
    with pytest.raises(CalendarCoverageError):
        write_day_view(redis, "stock_extra", "9999-12-31", real_rows(), source="baostock", version="v1", available_at="2026-10-28T23:01:00+08:00", fields=["symbol"])
    assert redis.pipelines == []


@pytest.mark.live
@pytest.mark.parametrize("family", ["stock_extra", "chip_peaks", "kline_ready", "dde_ready"])
def test_real_redis_view_sample_roundtrip(family):
    """真实 Redis + 真实视图样本。"""
    import os
    from uuid import uuid4

    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not set")
    import redis as redis_module

    path = Path(__file__).parent / f"fixtures/real/redis_view_{family}_2026-11-09.json"
    sample = json.loads(path.read_text())
    client = redis_module.Redis.from_url(url, decode_responses=True)
    key = f"core:test:C946:{family}:{uuid4().hex}"
    pipe = client.pipeline()
    pipe.hset(key, mapping=sample)
    pipe.expire(key, 60)
    pipe.execute()
    assert client.hlen(key) == len(sample) == 501
    assert client.hgetall(key) == sample
    assert 0 < client.ttl(key) <= 60
