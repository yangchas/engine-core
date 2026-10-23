"""Parsers are tested against RAW upstream responses captured by examples/capture_real_fixture.py.

Offline tests replay those captures; `--live` tests call the real sources and fail when the upstream
shape drifts from the committed capture. Hand-written rows are not accepted here (docs/TESTING.md).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider

REAL = Path(__file__).parent / "fixtures" / "real"
PROVENANCE = json.loads((REAL / "PROVENANCE.json").read_text(encoding="utf-8"))
INDEX = {name: meta for name, meta in PROVENANCE.items()
         if isinstance(meta, dict) and meta.get("kind") == "raw_upstream_response"}
CORE_ARTIFACT_INDEX = PROVENANCE.get("_core_artifacts", {})
SOURCES = {"ths_hot_rank", "kaipan_hot_plates", "kaipan_pool"}
CREDENTIAL_WORDS = ("token", "password", "secret", "cookie", "authorization")


def load(prefix: str) -> dict:
    name = sorted(n for n in INDEX if n.startswith(prefix))[-1]
    return json.loads((REAL / name).read_text(encoding="utf-8"))


def test_every_real_fixture_has_matching_provenance():
    files = {p.name for p in REAL.glob("*.json") if p.name != "PROVENANCE.json"}
    assert files == set(INDEX), "fixture files and PROVENANCE.json must list the same files"
    for name, meta in INDEX.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["source"] in SOURCES and meta["kind"] == "raw_upstream_response"
        assert meta["fetched_at"] and meta["command"].startswith("examples/capture_real_fixture.py")
        assert not any(word in body.decode("utf-8").lower() for word in CREDENTIAL_WORDS), name
    core_root = REAL / "core_artifacts"
    core_files = {p.relative_to(REAL).as_posix() for p in core_root.rglob("*") if p.is_file()}
    assert core_files == set(CORE_ARTIFACT_INDEX)
    for name, meta in CORE_ARTIFACT_INDEX.items():
        body = (REAL / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], name
        assert meta["bytes"] == len(body) and meta["source_path"] and meta["command"].startswith("cp ")
        assert meta["kind"] in {"core_artifact_manifest", "core_artifact_rows"}
        if meta["kind"] == "core_artifact_manifest":
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
    out = KaipanProvider(ReplayKaipan(plates=raw)).hot_plates(raw["Day"][0])
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


@pytest.mark.live
def test_live_ths_hot_rank_matches_captured_schema():
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
    if fresh is None:
        pytest.skip("UPSTREAM_UNREACHABLE: ths hot_list endpoint did not answer in 4 tries")
    assert ThsHotRankProvider.normalize(fresh)
    assert set(fresh["data"]["stock_list"][0]) == set(load("ths_hot_rank_")["data"]["stock_list"][0])
