import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine_core.data_pipeline.access import get
from engine_core.data_pipeline.capture.quote_slice import capture_quote_slice
from engine_core.data_pipeline.registry.quote_slice import SPEC
from engine_core.data_pipeline.storage import ArtifactCatalog
from tests.replay_quote_slice import load_v0, replay_clients

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="session")
def v0_raw():
    return load_v0()


@pytest.fixture
def replay(v0_raw):
    return replay_clients(v0_raw)


def _capture(replay, root, *, tag="0925", now=None, sources=("redis_q2", "td_auction_snapshot_v2")):
    redis_client, td_client = replay
    return capture_quote_slice("2026-10-23", tag, redis_client=redis_client,
                               td_client=td_client, catalog=ArtifactCatalog(root),
                               now=now, sources=sources)


def test_quote_slice_declares_configurable_source_preference():
    assert SPEC.source_preference == (
        "redis_q2",
        "td_auction_snapshot_v2",
        "wencai_auction",
    )


def test_real_v0_capture_commits_both_sources_with_observational_gates(replay, tmp_path):
    result = _capture(replay, tmp_path / "core",
                      now=datetime(2026, 10, 23, 9, 25, 35, tzinfo=SHANGHAI))
    assert result["failed"] == {}
    redis, td = result["committed"]["redis_q2"], result["committed"]["td_auction_snapshot_v2"]
    assert redis["row_count"] == 5226 and td["row_count"] == 5224
    assert redis["gate"]["gate_status"] in {"OK", "WARN"}
    assert td["gate"]["gate_status"] in {"OK", "WARN"}
    for item in (redis, td):
        flags = item["gate"]["flags_by_field"]
        assert len(flags) < 10 and all(0 <= count <= item["row_count"] for count in flags.values())
        assert item["gate"]["key_invalid"] == 0
        assert item["quality"]["coverage"] == 1.0
    assert redis["missing_hashes"] == {"count": 0, "symbols": []}
    assert replay[1].query.lower().startswith("select ")
    assert "select ts,px_milli,chg_bp,match_amt_yuan,rest_bid_amt_yuan,rest_ask_amt_yuan,limit_state,symbol,trade_date,auction_tag" in replay[1].query


def test_real_v0_capture_is_idempotent(replay, tmp_path):
    root = tmp_path / "core"
    now = datetime(2026, 10, 23, 9, 25, 35, tzinfo=SHANGHAI)
    first = _capture(replay, root, now=now)
    second = _capture(replay, root, now=now)
    assert {source: item["version"] for source, item in first["committed"].items()} == {
        source: item["version"] for source, item in second["committed"].items()}
    entries = ArtifactCatalog(root).read()["datasets"]["quote_slice"]["dates"]["2026-10-23"]
    assert len(entries) == 2


def test_real_v0_failure_isolated_to_redis_source(replay, tmp_path):
    class FailedRedis:
        def smembers(self, _key):
            raise ConnectionError("replay Redis unavailable")

    result = capture_quote_slice("2026-10-23", "0925", redis_client=FailedRedis(),
                                 td_client=replay[1], catalog=ArtifactCatalog(tmp_path / "core"),
                                 now=datetime(2026, 10, 23, 9, 25, 35, tzinfo=SHANGHAI))
    assert result["failed"]["redis_q2"]["error_type"] == "ConnectionError"
    assert result["committed"]["td_auction_snapshot_v2"]["row_count"] == 5224


def test_real_v0_capture_marks_late_reads(replay, tmp_path):
    result = _capture(replay, tmp_path / "core",
                      now=datetime(2026, 10, 23, 9, 26, tzinfo=SHANGHAI))
    assert {item["business_status"] for item in result["committed"].values()} == {"LATE_CAPTURE"}


def test_get_can_filter_source_and_uses_preference_before_availability(replay, tmp_path):
    root = tmp_path / "core"
    result = _capture(replay, root, now=datetime(2026, 10, 23, 9, 25, 35, tzinfo=SHANGHAI))
    assert result["failed"] == {}
    catalog = ArtifactCatalog(root)
    for item in result["committed"].values():
        manifest = Path(item["manifest_uri"])
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["gate"]["gate_status"] = "OK"
        data["quality"]["coverage"] = 1.0
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    default = get("quote_slice", trade_date="2026-10-23", tag="0925", root=root)
    explicit = get("quote_slice", trade_date="2026-10-23", tag="0925",
                   source="td_auction_snapshot_v2", root=root)
    assert default.meta["source"] == "redis_q2"
    assert explicit.meta["source"] == "td_auction_snapshot_v2"
    redis_entry = next(entry for entry in catalog.read()["datasets"]["quote_slice"]["dates"]["2026-10-23"]
                       if entry["source"] == "redis_q2")
    redis_manifest = Path(redis_entry["manifest"])
    data = json.loads(redis_manifest.read_text(encoding="utf-8"))
    data["gate"]["gate_status"] = "FAIL"
    redis_manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    failed_redis = get("quote_slice", trade_date="2026-10-23", tag="0925", root=root)
    assert failed_redis.meta["source"] == "td_auction_snapshot_v2"
