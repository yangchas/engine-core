"""C577: ladder_daily TD sink - in-memory twin (always) and a REAL TDengine temp database ``core_dev_c577_<pid>``."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from ladder_support import request

from engine_core.data_pipeline.ladder_daily import build_ladder_daily
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.td_ladder_sink import (LadderTdSink, MemoryLadderStore, TaosLadderStore, map_rows)
from engine_core.data_pipeline.td_sink import TdUnavailable, TdVerifyError

CFG = Path("/home/exedev/validation/operator-runs/taos-cfg")


def commit(root, day="2026-10-27", status_override=None, **kw):
    part = build_ladder_daily(request(day, **kw))
    manifest = dict(part.manifest)
    if status_override:
        manifest["business_status"] = status_override
    now = datetime.fromisoformat(f"{day}T23:30:00+08:00")
    return ArtifactCatalog(root).commit("ladder_daily", day, part.rows, manifest, source=manifest["provider"],
                                        clock=lambda: now)


def test_column_mapping_is_the_documented_one(tmp_path):
    m = commit(tmp_path)
    rows = ArtifactCatalog(tmp_path).read_rows(Path(m["manifest_uri"]))
    mapped = map_rows(rows)
    row = rows[0]
    assert mapped[row["symbol"]] == {"limit_up": True, "streak_n": row["streak_n"],
                                      "streak_start_date": row["streak_start_date"],
                                      "day_source_chain": row["day_source_chain"], "quality": "OK"}


def test_sync_is_idempotent_and_versioned(tmp_path):
    store = MemoryLadderStore()
    sink = LadderTdSink(store, tmp_path)
    first = commit(tmp_path)
    out = sink.sync_date("2026-10-27")
    assert out.status == "synced" and out.version == first["version"] and out.row_count == first["row_count"]
    assert sink.sync_date("2026-10-27").status == "unchanged"                       # no new write
    calls = len(store.versions["2026-10-27"])
    sink.sync_date("2026-10-27")
    assert len(store.versions["2026-10-27"]) == calls
    second = commit(tmp_path, as_of="2026-10-29T00:00:00+08:00", source_ladder=["kaipan", "daily_kline"])
    assert second["version"] != first["version"]
    out = sink.sync_date("2026-10-27")
    assert out.status == "synced" and out.version == second["version"]               # replaced the day
    assert len(store.days["2026-10-27"]) == second["row_count"]
    assert {r["version"] for r in store.days["2026-10-27"].values()} == {second["version"]}


def test_lower_quality_never_replaces_higher(tmp_path):
    store = MemoryLadderStore()
    sink = LadderTdSink(store, tmp_path)
    commit(tmp_path, status_override="FORMAL")
    assert sink.sync_date("2026-10-27").status == "synced"
    commit(tmp_path, as_of="2026-10-29T00:00:00+08:00", source_ladder=["kaipan", "daily_kline"],
           status_override="DEGRADED")
    # catalog best is still the FORMAL one (rank first), so TD stays on it
    assert sink.sync_date("2026-10-27").status in ("unchanged", "skipped_lower")


def test_legacy_partition_is_not_copied_and_td_down_is_retryable(tmp_path):
    legacy = ArtifactCatalog(tmp_path).commit("ladder_daily", "2026-10-27", [{"symbol": "000001", "streak_n": 6}],
                                             {"dataset": "ladder_daily", "trade_date": "2026-10-27"},
                                             source="core_price_close_proxy")
    assert legacy
    store = MemoryLadderStore()
    assert LadderTdSink(store, tmp_path).sync_date("2026-10-27").status == "legacy_only"
    commit(tmp_path)
    store.fail = "unavailable"
    with pytest.raises(TdUnavailable):
        LadderTdSink(store, tmp_path).sync_date("2026-10-27")


def test_readback_mismatch_fails_verification(tmp_path):
    class Lossy(MemoryLadderStore):
        def replace_day(self, day, version, mapped):
            super().replace_day(day, version, mapped)
            first = sorted(self.days[day])[0]
            self.days[day][first]["streak_n"] += 1          # a wrong column value written by the store
    commit(tmp_path)
    with pytest.raises(TdVerifyError):
        LadderTdSink(Lossy(), tmp_path).sync_date("2026-10-27")


@pytest.fixture(scope="module")
def td():
    if not os.environ.get("TD_CONFIG") and CFG.exists():
        os.environ["TD_CONFIG"] = str(CFG)
    try:
        import taos  # noqa: F401
        probe = TaosLadderStore("core_dev_c577_probe_" + str(os.getpid()))
        probe._cursor()
        probe.close()
    except (ImportError, TdUnavailable) as exc:
        pytest.skip("no reachable TDengine: " + str(exc)[:80])
    store = TaosLadderStore("core_dev_c577_" + str(os.getpid()))
    yield store
    store.drop_database()
    check = TaosLadderStore(store.database)
    assert check.schema_exists() is False
    check.close()
    store.close()


def test_real_td_roundtrip_and_replace(tmp_path, td):
    td.drop_database()
    td.ensure_schema()
    cols = {r[0]: r[1] for r in td._run(f"DESCRIBE {td.database}.ladder_daily_v1")}
    assert cols["limit_up"] == "BOOL" and cols["streak_n"] == "INT" and cols["symbol"] == "VARCHAR"
    sink = LadderTdSink(td, tmp_path)
    first = commit(tmp_path)
    out = sink.sync_date("2026-10-27")
    assert out.status == "synced" and td.day_aggregates("2026-10-27")["count"] == first["row_count"]
    assert sink.sync_date("2026-10-27").status == "unchanged"
    rows = td.read_day_rows("2026-10-27", ["600838", "002724"])
    assert rows["600838"]["streak_n"] == 5 and rows["600838"]["version"] == first["version"]
    assert "002724" in rows and rows["002724"]["streak_n"] == 1
    commit(tmp_path, as_of="2026-10-29T00:00:00+08:00", source_ladder=["kaipan", "daily_kline"])
    out = sink.sync_date("2026-10-27")
    assert out.status == "synced"
    assert td.day_aggregates("2026-10-27")["count"] == out.row_count          # old rows of the day did not survive
    assert len(td.read_versions("2026-10-27")) == 2
