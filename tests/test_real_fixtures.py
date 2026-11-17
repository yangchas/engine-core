"""Parsers are tested against RAW upstream responses captured by examples/capture_real_fixture.py.

Offline tests replay those captures; `--live` tests call the real sources and fail when the upstream
shape drifts from the committed capture. Hand-written rows are not accepted here (docs/TESTING.md).
"""
from __future__ import annotations

import hashlib
import gzip
import inspect
import json
import os
from pathlib import Path

import pytest

from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider

REAL = Path(__file__).parent / "fixtures" / "real"
PROVENANCE = json.loads((REAL / "PROVENANCE.json").read_text(encoding="utf-8"))
INDEX = {name: meta for name, meta in PROVENANCE.items()
         if isinstance(meta, dict) and meta.get("kind") == "raw_upstream_response"}
CORE_ARTIFACT_INDEX = PROVENANCE.get("_core_artifacts", {})
C526_EXCERPT_INDEX = PROVENANCE.get("_c526_excerpts", {})
SOURCES = {"ths_hot_rank", "kaipan_hot_plates", "kaipan_pool", "wencai_daily_kline",
           "kaipan_ban_reasons", "wencai_limit_up_pool",
           "wencai_limit_up_pool_today", "q2_limit_up_pool_today", "kaipan_plate_members", "kaipan_stock_dde", "baostock_query_all_stock",
           "baostock_stock_basic", "baostock_daily_trade_status", "td_stock_tick", "td_stock_static", "redis_auction_full_row", "t1v2_replay_row"}
CREDENTIAL_WORDS = ("token", "password", "secret", "cookie", "authorization")


def load(prefix: str) -> dict:
    name = sorted(n for n in INDEX if n.startswith(prefix))[-1]
    return json.loads((REAL / name).read_text(encoding="utf-8"))


def test_every_real_fixture_has_matching_provenance():
    for item in PROVENANCE['_c1326']:
        assert hashlib.sha256((REAL / item['file']).read_bytes()).hexdigest() == item['sha256']
        assert item['source'] and item['source_sha256'] and item['selection']
    c1324 = PROVENANCE['_c1324']
    body = (REAL / c1324['file']).read_bytes()
    assert hashlib.sha256(body).hexdigest() == c1324['sha256']
    assert len(body) == c1324['bytes'] and len(json.loads(body)['rows']) == c1324['rows'] == 243
    assert c1324['source'] and c1324['fetched_at']
    c1317 = PROVENANCE['_c1317']
    body = (REAL / c1317['file']).read_bytes()
    assert hashlib.sha256(body).hexdigest() == c1317['sha256']
    assert sum(len(json.loads('[' + line.split('[', 1)[1])) for line in body.decode().splitlines()) == c1317['row_count'] == 11
    assert c1317['source'] and c1317['source_sha256'] and c1317['selection']
    c1312 = PROVENANCE['_c1312']
    c1312_body = (REAL / c1312['file']).read_bytes()
    assert hashlib.sha256(c1312_body).hexdigest() == c1312['sha256']
    c1312_data = json.loads(c1312_body)
    assert len(c1312_data['markers']) == c1312['marker_count'] == 2
    assert len(c1312_data['rows']) == c1312['row_count'] == 40
    assert c1312['sql'] and c1312['source'] and c1312['fetched_at']
    c1302 = PROVENANCE["_c1302"]
    assert hashlib.sha256((REAL / c1302["file"]).read_bytes()).hexdigest() == c1302["sha256"]
    assert c1302["keys"] and c1302["fetched_at"] and c1302["source"]
    c1299 = PROVENANCE["_c1299"]
    c1299_body = (REAL / c1299["file"]).read_bytes()
    assert hashlib.sha256(c1299_body).hexdigest() == c1299["sha256"]
    assert len(json.loads(c1299_body)["rows"]) == c1299["row_count"] == 7
    assert c1299["sql"] and c1299["source"] and c1299["fetched_at"]
    c1287 = PROVENANCE["_c1287"]
    c1287_body = (REAL / c1287["file"]).read_bytes()
    assert hashlib.sha256(c1287_body).hexdigest() == c1287["sha256"]
    assert len(c1287_body) == c1287["bytes"] and len(json.loads(c1287_body)) == c1287["rows"]
    assert c1287["sql"] and c1287["source"] and c1287["fetched_at"]
    captured = PROVENANCE["_c1280"]
    body = (REAL / captured["file"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == captured["sha256"]
    assert len(body) == captured["bytes"] and len(json.loads(body)["rows"]) == captured["rows"] == 243
    # These files describe fixtures rather than containing raw upstream responses.
    files = {p.name for p in REAL.glob("*.json")
             if p.name != "PROVENANCE.json" and not p.name.endswith("_PROVENANCE.json")}
    assert files == set(INDEX), "fixture files and PROVENANCE.json must list the same files"
    for name, meta in INDEX.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["source"] in SOURCES and meta["kind"] == "raw_upstream_response"
        assert meta["fetched_at"] and meta["command"].startswith("examples/capture_real_fixture.py")
        assert not any(word in body.decode("utf-8").lower() for word in CREDENTIAL_WORDS), name


def test_c526_real_excerpts_have_matching_provenance():
    excerpt_root = REAL / "c526"
    files = {p.relative_to(REAL).as_posix() for p in excerpt_root.rglob("*") if p.is_file()}
    assert files == set(C526_EXCERPT_INDEX)
    for name, meta in C526_EXCERPT_INDEX.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["bytes"] == len(body) and meta["kind"] == "raw_upstream_excerpt"
        assert meta["source_path"] and meta["source_sha256"] and meta["selection"] and meta["command"]
        assert meta["fetched_at"] and meta["trade_date"] == "2026-10-23"
        checked_body = gzip.decompress(body) if name.endswith(".gz") else body
        assert not any(word in checked_body.decode("utf-8").lower() for word in CREDENTIAL_WORDS), name
    core_root = REAL / "core_artifacts"
    core_files = {p.relative_to(REAL).as_posix() for p in core_root.rglob("*") if p.is_file()}
    assert core_files == set(CORE_ARTIFACT_INDEX)
    for name, meta in CORE_ARTIFACT_INDEX.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["bytes"] == len(body) and meta["source_path"]
        if meta["kind"] == "core_artifact_rows_excerpt":
            assert meta["command"].startswith("extract first original JSONL row")
            assert meta["selection"] == "first original JSONL row" and meta["row_count"] == 1
            rows = [json.loads(line) for line in gzip.decompress(body).splitlines() if line]
            assert len(rows) == meta["row_count"]
            assert {"source", "available_at"} <= set(rows[0])
            assert not any(word in json.dumps(rows[0]).lower() for word in CREDENTIAL_WORDS), name
        else:
            assert meta["command"].startswith("cp ")
            assert meta["kind"] in {"core_artifact_manifest", "core_artifact_rows", "core_catalog_snapshot"}
        if meta["kind"] in {"core_artifact_manifest", "core_catalog_snapshot"}:
            assert not any(word in body.decode("utf-8").lower() for word in CREDENTIAL_WORDS), name


def test_ths_hot_rank_normalize_real_payload():
    payload = load("ths_hot_rank_")
    rows = ThsHotRankProvider.normalize(payload)
    valid = [i for i in payload["data"]["stock_list"] if str(i.get("code") or "")[-6:].isdigit()]
    assert len(rows) == len(valid) >= 50
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(len(r["symbol"]) == 6 and r["symbol"].isdigit() and r["name"] for r in rows)
    assert all(isinstance(r.get("heat"), float) for r in rows)


class ReplayKaipan:
    """Replays captured Kaipan responses; the parser under test is the real KaipanProvider."""

    def __init__(self, plates=None, pool_by_level=None):
        self.plates, self.pool_by_level = plates, pool_by_level or {}

    def get_his_plates(self, day, index, size):
        return self.plates

    def get_his_bans(self, day, level):
        return self.pool_by_level.get(level, {"errcode": "1020", "errmsg": "参数出错"})


def test_kaipan_hot_plates_parse_real_response():
    raw = load("kaipan_hot_plates_")
    out = KaipanProvider(ReplayKaipan(plates=raw)).hot_plates(raw["Day"][0], max_pages=1)
    assert out["rows"] and len(out["rows"]) == min(len(raw["list"]), 50)
    assert out["response_day"] == raw["Day"] and out["requested_date"] == raw["Day"][0]
    first = out["rows"][0]
    assert first["plate_code"] == raw["list"][0][0] and first["name"] == raw["list"][0][1]
    assert all(r["plate_code"] and r["plate_code"].isdigit() for r in out["rows"])


def test_kaipan_hot_plates_rejects_wrong_day_real_response():
    raw = load("kaipan_hot_plates_")
    with pytest.raises(ValueError, match="response_date_mismatch"):
        KaipanProvider(ReplayKaipan(plates=raw)).hot_plates("2026-01-05")


def test_kaipan_limit_up_pool_real_responses():
    levels = {1: load("kaipan_pool_2026-10-22_L1_"), 2: load("kaipan_pool_2026-10-22_L2_")}
    out = KaipanProvider(ReplayKaipan(pool_by_level=levels)).limit_up_pool("2026-10-22")
    assert out["rows"] and {r["lb_days"] for r in out["rows"]} >= {1, 2}
    assert len({r["symbol"] for r in out["rows"]}) == len(out["rows"])


def test_kaipan_pool_unavailable_day_is_real_error_shape():
    unavailable = load("kaipan_pool_2026-10-23_L1_")
    assert str(unavailable.get("errcode")) == "1020" and "info" not in unavailable


class ReplayKaipanPlateMembers:
    def __init__(self, response):
        self.response = response

    def get_his_plate_ids(self, plate, day, index, size):
        assert index == "0"
        return self.response


def test_kaipan_plate_members_parse_ten_captured_real_responses():
    paths = sorted(REAL.glob("kaipan_plate_members_2026-10-28_*_i0_*.json"))
    assert len(paths) == 10
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        plate = path.name.split("_")[4]
        out = KaipanProvider(
            ReplayKaipanPlateMembers(raw), plate_codes=(plate,), max_requests=1,
            min_interval_seconds=0, sleep_fn=lambda _: None,
        ).plate_members("2026-10-28", page_size=2000, concurrency=1)
        expected_symbols = [str(row[0]).zfill(6) for row in raw["list"]]
        assert not out["partial"], path.name
        assert out["request_count"] == 1, path.name
        assert len(out["rows"]) == int(raw["Count"]) == len(expected_symbols), path.name
        assert [row["symbol"] for row in out["rows"]] == expected_symbols, path.name


def test_kaipan_membership_adapter_has_no_checkpoint_api_and_replays_real_rows(tmp_path, monkeypatch):
    checkpoint_options = {"members_checkpoint_path", "reasons_checkpoint_path"}
    assert checkpoint_options.isdisjoint(inspect.signature(KaipanProvider.__init__).parameters)
    monkeypatch.chdir(tmp_path)
    path = sorted(REAL.glob("kaipan_plate_members_2026-10-28_*_i0_*.json"))[0]
    raw = json.loads(path.read_text(encoding="utf-8"))
    plate = path.name.split("_")[4]
    out = KaipanProvider(
        ReplayKaipanPlateMembers(raw), plate_codes=(plate,), max_requests=1,
        min_interval_seconds=0, sleep_fn=lambda _: None,
    ).plate_members("2026-10-28", page_size=2000, concurrency=1)
    assert not out["partial"]
    assert len(out["rows"]) == int(raw["Count"])
    assert not list(tmp_path.rglob("*.json"))


def test_kaipan_dde_builder_uses_echoed_dates_and_preserves_missing_as_null():
    from engine_core.data_pipeline.daily_dde import build_daily_dde
    from engine_core.data_pipeline.daily_dde import DdeStaleResponse

    raw = load("kaipan_stock_dde_2026-10-29_000001_")
    partition = build_daily_dde(raw, symbol="000001", target_date="2026-10-28", history_trading_days=250)
    assert len(partition.rows) == 250
    dates = [row["date"] for row in partition.rows]
    assert dates[0] == "2026-10-28"
    assert dates == sorted(dates, reverse=True)
    from datetime import date
    assert (date.fromisoformat(dates[0]) - date.fromisoformat(dates[-1])).days > 250
    assert all(row["symbol"] == "000001" for row in partition.rows)
    assert all(set(row) == {"symbol", "date", "ddje"} for row in partition.rows)
    assert len({row["date"] for row in partition.rows}) == 250
    assert partition.manifest["source_dates"]["date"] == dates
    assert partition.manifest["field_units"]["ddje"] == "yuan"
    assert partition.manifest["field_units"] == {"ddje": "yuan"}
    with pytest.raises(DdeStaleResponse, match="stale|target_date_missing"):
        build_daily_dde(raw, symbol="000001", target_date="2026-10-29", history_trading_days=250)


def test_run_data_jobs_does_not_treat_missing_real_read_as_ready():
    from examples.run_data_jobs import _dataset_read_ready
    from engine_core.data_pipeline.reader import DatasetRead

    load("kaipan_pool_")
    missing = DatasetRead(rows=[], manifest={}, discovered_by="none", status="MISSING")
    assert _dataset_read_ready(missing) is False


@pytest.mark.live
def test_live_source_contracts_match_captured_schemas(tmp_path):
    redis_url, td_config = os.environ.get("REDIS_URL"), os.environ.get("TD_CONFIG")
    if not redis_url or not td_config:
        pytest.skip("UPSTREAM_UNREACHABLE: REDIS_URL or TD_CONFIG unavailable")
    import redis
    import taos
    from engine_core.data_pipeline.capture.quote_slice import capture_quote_slice
    from engine_core.data_pipeline.storage import ArtifactCatalog
    redis_client = redis.Redis.from_url(redis_url, socket_timeout=3, socket_connect_timeout=3)
    td_client = taos.connect(database="market_data1", config=td_config)
    try:
        captured = capture_quote_slice(
            "2026-10-23", "0925", redis_client=redis_client, td_client=td_client,
            catalog=ArtifactCatalog(tmp_path / "quote_slice"))
    finally:
        redis_client.close()
        td_client.close()
    assert captured["failed"] == {}
    assert captured["committed"]["redis_q2"]["row_count"] == 5226
    assert captured["committed"]["td_auction_snapshot_v2"]["row_count"] == 5224

    from engine_core.data_pipeline.providers import ths_hot_rank as ths
    import urllib.parse
    import urllib.request
    url = ths.URL + "?" + urllib.parse.urlencode({"stock_type": "a", "type": "day", "list_type": "tech"})
    fresh = None
    for _ in range(4):  # endpoint is flaky (SSL handshake timeouts seen 2026-10-23)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=ths.HEADERS), timeout=30) as response:
                fresh = json.loads(response.read().decode("utf-8"))
            break
        except OSError:
            continue
    if fresh is not None:
        assert ThsHotRankProvider.normalize(fresh)
        assert set(fresh["data"]["stock_list"][0]) == set(load("ths_hot_rank_")["data"]["stock_list"][0])
    else:
        pytest.skip("UPSTREAM_UNREACHABLE: ths hot_list endpoint did not answer in 4 tries")


def test_td_recordings_have_matching_provenance():
    """Every recorded TD result / error under fixtures/real/td is registered with its sha256 (C663)."""
    recordings = PROVENANCE.get("_td_recordings", {})
    root = REAL / "td"
    files = {p.relative_to(REAL).as_posix() for p in root.rglob("*") if p.is_file()}
    assert files == set(recordings)
    for name, meta in recordings.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["kind"] in {"raw_td_result", "raw_td_error", "raw_td_function_output"} and meta["command"] and meta.get("source"), name
        text = (gzip.decompress(body) if name.endswith(".gz") else body).decode("utf-8").lower()
        assert not any(word in text for word in CREDENTIAL_WORDS), name


def test_c852_fixture_manifest_integrity():
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    assert not list(root.rglob('*.py'))
    manifest = json.loads((root / 'MANIFEST.json').read_text(encoding='utf-8'))
    assert manifest['id'] == 'C852'
    assert '不是上游数据' in (root / 'PROVENANCE.md').read_text(encoding='utf-8')
    for name, meta in manifest['files'].items():
        body = (root / name).read_bytes()
        assert len(body) == meta['bytes'], name
        assert hashlib.sha256(body).hexdigest() == meta['sha256'], name


def test_c860_reference_provenance_integrity():
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    hashes = {
        'expected_skip_suspended.jsonl': 'bc40f9ad82dcf33b1e42c2f9d3e7e1fcd28f5d957129d3890699b8acfb92a6e5',
        'PROVENANCE_skip_suspended.md': 'b529a882f9a27982abb28b9a4a6df59b5fae4008f493d653093e88217ef51a0a',
    }
    for name, digest in hashes.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    assert '不是上游数据' in (root / 'PROVENANCE_skip_suspended.md').read_text()


def test_c861_reference_provenance_integrity():
    root = Path(__file__).parent / 'fixtures' / 'price_volume_c852'
    hashes = {
        'expected_batch2.jsonl': '20ad0eb74a93cc3c982a48b9f83b0c445526ab4a8663a9e678d59497e93280da',
        'PROVENANCE_batch2.md': '098c649d347a9e6661452cc2399c5f63ed41b646c5b6539eea17a137ae53bb20',
    }
    for name, digest in hashes.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    assert '不是上游数据' in (root / 'PROVENANCE_batch2.md').read_text()


def test_c879_reference_provenance_integrity():
    root = Path(__file__).parent / 'fixtures' / 'chip_peak_c879'
    hashes = {
        'MANIFEST.json': '18934b5cdd1d9766dc078a9184f0d2107563119fc679238fc32cf60b8f6015bb',
        'PROVENANCE_chip.md': '43c306a24586f9496855f25a0230750e4acaf1d8e88839b1c47ce435dfd72377',
        'boundary_cases.jsonl': '226a4d56aadac9c1ba9d116dd59804c055579d0a269598ec62b12ee930cb7dc4',
        'expected_chip.jsonl': 'caff56141e194daaa2535d60de1abb80c7ae1cec8af391659e59496f2dae6376',
        'expected_chip_legacy.jsonl': 'dd5078438a9c9bd19c85916a04d94b6943e65cce30ae9b8ba500b1720daa295b',
    }
    assert {p.name for p in root.iterdir()} == set(hashes) | {'SHA256SUMS'}
    assert not list(root.rglob('*.py'))
    for name, digest in hashes.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_hot_rank_real_snapshot_two_layer(tmp_path, monkeypatch):
    from engine_core.data_pipeline.jobs import JobRunner, JobState
    from engine_core.storage.two_layer import TwoLayerStore

    class RedisMemory:
        def __init__(self):
            self.values = {}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, **kwargs):
            self.values[key] = value
            self.last_set = (key, kwargs)
            return True

    class EmptyCold:
        def latest_version(self, dataset, day):
            return None

    from engine_core.data_pipeline import cold_archive, hot_rank_view
    monkeypatch.setattr(hot_rank_view, "hot_view_expire_at", lambda day: 2000000000)
    archives = []
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: archives.append(args) or True)
    redis = RedisMemory()
    rows = ThsHotRankProvider.normalize(load("ths_hot_rank_"))
    store = TwoLayerStore(redis, cold_store=EmptyCold())
    assert store.get("hot_rank", "2026-10-23").status == "MISSING"
    result = JobRunner(artifact_root=tmp_path, redis=redis, state=JobState()).run(
        "hot_rank", "2026-10-23", fetch=lambda _: rows,
        now="2026-10-23T14:41:00+08:00", source="ths_hot_rank", publish=True)
    assert result.manifest["cache_publish"] == "READY"
    assert len(store.get("hot_rank", "2026-10-23").rows) == len(rows)
    assert not (tmp_path / "catalog.json").exists()
    assert store.presence("hot_rank", "2026-10-23").status == "PRESENT_REDIS"
    assert len(archives) == 1 and archives[0][0] == "hot_rank"
    class ArchivedCold:
        def latest_version(self, dataset, day):
            return archives[0][2]

    monkeypatch.setattr(cold_archive, "read_envelope", lambda *args, **kwargs: archives[0][3])
    store.cold = ArchivedCold()
    key = "core:view:hot_rank:2026-10-23"
    del redis.values[key]
    restored = store.get("hot_rank", "2026-10-23")
    assert restored.status == "RESTORED_FROM_TD" and len(restored.rows) == len(rows)
    assert hot_rank_view.validate(redis.get(key), "2026-10-23")["rows"] == restored.rows
    assert redis.last_set == (key, {"exat": 2000000000, "nx": True})
    del redis.values[key]
    assert store.presence("hot_rank", "2026-10-23").status == "RESTORED_FROM_TD"
    assert redis.get(key) is not None
    del redis.values[key]
    original_set = redis.set
    monkeypatch.setattr(redis, "set", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("restore_failed")))
    assert store.get("hot_rank", "2026-10-23").status == "RESTORED_FROM_TD"
    monkeypatch.setattr(redis, "set", original_set)
    redis.set(key, archives[0][3])
    value = store.get("hot_rank", "2026-10-23")
    assert value.metadata['available_at'].endswith('+08:00')
    assert store.get("hot_rank", "2026-10-23", asof="2026-10-23T14:40:00+08:00").status == "MISSING"
    redis.values['core:view:hot_rank:2026-10-23'] = '{}'
    assert store.presence("hot_rank", "2026-10-23").status == "CORRUPT"
    direct = store.publish("hot_rank", "2026-10-23", rows, result.manifest)
    assert direct.archive_status == 'QUEUED'
    assert len(store.get("hot_rank", "2026-10-23").rows) == len(rows)

    class FailingNewView(RedisMemory):
        def set(self, key, value, **kwargs):
            if key.startswith('core:view:hot_rank:'):
                raise RuntimeError('new_view_failed')
            return super().set(key, value, **kwargs)

    failed_redis = FailingNewView()
    with pytest.raises(RuntimeError, match='new_view_failed'):
        JobRunner(artifact_root=tmp_path / 'failure', redis=failed_redis, state=JobState()).run(
            "hot_rank", "2026-10-23", fetch=lambda _: rows,
            now="2026-10-23T14:41:00+08:00", source="ths_hot_rank", publish=True)
    assert not (tmp_path / 'failure' / 'catalog.json').exists()
    assert failed_redis.get('core:cache:data:hot_rank:2026-10-23') is None



def test_hot_rank_readers_use_two_layer_real_snapshot(tmp_path, monkeypatch):
    from engine_core.data_pipeline import cold_archive, hot_rank_view
    from engine_core.data_pipeline.jobs import JobRunner, JobState
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.prep_trade_day import _latest_step
    from engine_core.slices import SliceProviders, SliceReader

    class RedisMemory:
        def __init__(self): self.values = {}
        def get(self, key): return self.values.get(key)
        def set(self, key, value, **kwargs): self.values[key] = value; return True
        def type(self, key): return 'none'
    class EmptyCold:
        def latest_version(self, dataset, day): return None
        def close(self): pass

    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    monkeypatch.setattr(cold_archive, 'TdStore', EmptyCold)
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: True)
    monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda day: 2000000000)
    redis = RedisMemory()
    catalog = ArtifactCatalog(tmp_path)
    day, asof = '2026-10-23', '2099-10-23T15:00:00+08:00'
    rows = ThsHotRankProvider.normalize(load('ths_hot_rank_'))
    JobRunner(artifact_root=tmp_path, redis=redis, state=JobState()).run(
        'hot_rank', day, fetch=lambda _: rows, now='2026-10-23T14:41:00+08:00',
        source='ths_hot_rank', publish=True)
    def check():
        sliced = SliceReader(SliceProviders(catalog=catalog, redis=redis).providers()).get('hot_rank', trade_date=day, asof=asof)
        prep = _latest_step('hot_rank', 'hot_rank', day, catalog, asof=asof, redis=redis)
        return sliced, prep
    sliced, prep = check()
    assert len(sliced.rows) == prep['row_count'] == len(rows)
    redis.values.pop('core:view:hot_rank:' + day)
    sliced, prep = check()
    assert sliced.meta['status'] == prep['status'] == 'MISSING'
    assert sliced.rows == [] and prep['trade_date'] is None
    redis.values['core:view:hot_rank:' + day] = '{}'
    sliced, prep = check()
    assert sliced.meta['status'] == 'DEGRADED' and sliced.meta['reason'].startswith('CORRUPT:')
    assert prep['status'] == 'CORRUPT'
    redis.values.pop('core:view:hot_rank:' + day)
    monkeypatch.setattr(cold_archive, '_enabled', lambda: False)
    sliced, prep = check()
    assert sliced.meta['status'] == prep['status'] == 'UNKNOWN'


def test_hot_rank_legacy_fallback_marks_unknown_store(tmp_path, monkeypatch):
    from engine_core.data_pipeline import cold_archive
    from engine_core.slices import SliceProviders, SliceReader

    rows = ThsHotRankProvider.normalize(load('ths_hot_rank_'))
    class RedisMemory:
        def get(self, key): return None
        def hgetall(self, key):
            return {row['symbol']: json.dumps(row) for row in rows} if key == 'cache:hot_rank:2026-10-23' else {}
    monkeypatch.setattr(cold_archive, '_enabled', lambda: False)
    result = SliceReader(SliceProviders(artifact_root=tmp_path, redis=RedisMemory()).providers()).get(
        'hot_rank', trade_date='2026-10-23', asof='2099-10-23T15:00:00+08:00')
    assert result.meta['status'] == 'UNKNOWN'
    assert result.meta['source'] == 'ths_hot_rank'
    assert result.meta['legacy_dependency'] is True
    assert result.meta['lineage'][0]['store_status'] == 'UNKNOWN'
    assert len(result.rows) == len(rows)


def test_hot_rank_publish_does_not_create_catalog(tmp_path, monkeypatch):
    from engine_core.data_pipeline import cold_archive, hot_rank_view
    from engine_core.data_pipeline.jobs import JobRunner, JobState

    class RedisMemory:
        def __init__(self): self.values = {}
        def get(self, key): return self.values.get(key)
        def set(self, key, value, **kwargs): self.values[key] = value; return True

    monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda day: 2000000000)
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: True)
    monkeypatch.setattr(cold_archive, 'flush', lambda: None)
    redis = RedisMemory()
    rows = ThsHotRankProvider.normalize(load('ths_hot_rank_'))
    result = JobRunner(artifact_root=tmp_path, redis=redis, state=JobState()).run(
        'hot_rank', '2026-10-23', fetch=lambda _: rows,
        now='2026-10-23T14:41:00+08:00', source='ths_hot_rank', publish=True)
    assert result.manifest['cache_publish'] == 'READY'
    assert redis.get('core:view:hot_rank:2026-10-23') is not None
    assert not (tmp_path / 'catalog.json').exists()
    assert not list(tmp_path.rglob('*.jsonl.gz'))


def test_hot_rank_guardian_presence_ignores_catalog_real_snapshot(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core.data_pipeline import cold_archive, hot_rank_view
    from engine_core.data_pipeline.jobs import JobRunner, JobState
    from engine_core.guardian import presence
    from engine_core.guardian.registry import load_registry, stale_items
    from engine_core.guardian.reconcile_plan import reconcile_plan

    class RedisMemory:
        def __init__(self): self.values = {}
        def get(self, key): return self.values.get(key)
        def set(self, key, value, **kwargs): self.values[key] = value; return True
    class EmptyCold:
        def latest_version(self, dataset, day): return None
    redis, cold = RedisMemory(), EmptyCold()
    archives = []
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: archives.append(args) or True)
    monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda day: 2000000000)
    day = '2026-10-23'
    rows = ThsHotRankProvider.normalize(load('ths_hot_rank_'))
    JobRunner(artifact_root=tmp_path, redis=redis, state=JobState()).run(
        'hot_rank', day, fetch=lambda _: rows, now=day + 'T14:41:00+08:00',
        source='ths_hot_rank', publish=True)
    resolve = presence.resolve_presence
    monkeypatch.setattr(presence, 'resolve_presence', lambda dataset, date, **kwargs:
                        resolve(dataset, date, redis_client=redis, cold_store=cold))
    item = next(item for item in load_registry() if item['id'] == 'hot_rank')
    now = datetime(2026, 10, 23, 15, tzinfo=ZoneInfo('Asia/Shanghai'))
    empty = {'datasets': {}}
    assert resolve('hot_rank', day, redis_client=redis, cold_store=cold) == 'PRESENT_REDIS'
    from engine_core.data_pipeline.reader import load_dataset
    from engine_core.data_pipeline.storage import ArtifactCatalog
    loaded = load_dataset('hot_rank', day, redis=redis, catalog=ArtifactCatalog(tmp_path))
    assert loaded.status == 'READY' and len(loaded.rows) == len(rows)
    raw = redis.values.pop('core:view:hot_rank:' + day)
    class ArchivedCold:
        def latest_version(self, dataset, date): return archives[0][2]
    monkeypatch.setattr(cold_archive, 'read_envelope', lambda *args, **kwargs: archives[0][3])
    assert resolve('hot_rank', day, redis_client=redis, cold_store=ArchivedCold()) == 'RESTORED_FROM_TD'
    assert not redis.values
    redis.values['core:view:hot_rank:' + day] = raw
    assert stale_items(now, empty, [item], trade_date=day)[0]['status'] == 'OK'
    assert reconcile_plan(now, empty, trade_date=day, prev_trade_date='2026-10-22',
                          phase='INTRADAY_PM', registry=[item]) == []
    redis.values.clear()
    assert stale_items(now, empty, [item], trade_date=day)[0]['status'] == 'OVERDUE'
    assert not any(row['action'] == 'FETCH_NOW' for row in reconcile_plan(
        now, empty, trade_date=day, prev_trade_date='2026-10-22', phase='INTRADAY_PM', registry=[item]))


def test_td_stock_static_rejects_invalid_symbol():
    from examples.capture_real_fixture import capture

    for symbol in (None, '12345', '1234567', '12a456', "12345'"):
        with pytest.raises(SystemExit, match='six-digit stock code'):
            capture('td_stock_static', '2026-11-16', 1, symbol=symbol)


def test_td_stock_static_real_rows_and_provenance():
    for symbol, listing_date in (('001381', '2026-11-03'), ('688827', '2026-11-10')):
        names = [name for name in INDEX if name.startswith(f'td_stock_static_2026-11-16_{symbol}_')]
        assert len(names) == 1
        meta = INDEX[names[0]]
        fixture = json.loads((REAL / names[0]).read_text(encoding='utf-8'))
        assert meta['source'] == 'td_stock_static' and meta['trade_date'] == '2026-11-16'
        assert meta['row_count'] == len(fixture['rows']) == 1
        assert f"symbol='{symbol}'" in meta['sql'] and 'core_data.stock_static_latest' in meta['sql']
        row = dict(zip(fixture['columns'], fixture['rows'][0], strict=True))
        payload = json.loads(row['payload'])
        assert row['symbol'] == symbol and payload['date'] == '2026-11-16'
        assert payload['listing_date'] == listing_date


def test_c1279_prep_excerpt_provenance():
    index = PROVENANCE["_c1279"]
    assert {p.relative_to(REAL).as_posix() for p in (REAL / "c1279").iterdir()} == set(index)
    for name, meta in index.items():
        body = (REAL / name).read_bytes()
        assert len(body) == meta["bytes"] and hashlib.sha256(body).hexdigest() == meta["sha256"]
        assert meta["source_path"] and meta["source_sha256"] and meta["selection"] and meta["command"]
        assert not any(word in body.decode("utf-8").lower() for word in CREDENTIAL_WORDS)


def test_baostock_daily_trade_status_rejects_invalid_parameters():
    from examples.capture_real_fixture import capture, main

    for symbol in (None, '12345', '1234567', '12a456', '１２３４５６'):
        with pytest.raises(SystemExit, match='six-digit stock code'):
            capture('baostock_daily_trade_status', '2026-11-16', 1, symbol=symbol)
    for day in ('2026-02-30', '2026-1-01', ''):
        with pytest.raises(SystemExit, match='valid date'):
            capture('baostock_daily_trade_status', day, 1, symbol='605255')
    with pytest.raises(SystemExit) as exc:
        main(['baostock_daily_trade_status', '--symbol', '605255'])
    assert exc.value.code == 2
def test_c1301_trade_status_excerpt_provenance():
    meta = PROVENANCE['_c1301']
    body = (REAL / meta['file']).read_bytes()
    raw = json.loads(body)
    assert hashlib.sha256(body).hexdigest() == meta['sha256']
    assert meta['source_path'] == '/home/exedev/fw/evidence/C1300/baostock-query.txt'
    assert meta['collector'] == 'C1300'
    assert meta['fetched_at'] == '2026-11-16T20:10:13.974870+08:00'
    assert meta['params'] == {'code': 'sh.605255', 'date': '2026-11-16', 'fields': raw['fields']}
    assert raw == {'fields': ['date', 'code', 'tradestatus'],
                   'rows': [['2026-11-16', 'sh.605255', '0']]}
