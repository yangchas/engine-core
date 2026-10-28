"""C580: the limit_up_pool TD sink against a REAL TDengine, in a throw-away ``core_dev_c580_<pid>`` database.

Never touches ``core_data`` or ``market_data1``.  The database is dropped at the end of the module (asserted).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine_core.data_pipeline import limit_pool as lp
from engine_core.data_pipeline.limit_pool_td import DATA_COLUMNS, LimitPoolTdSink, PoolTaosStore, map_rows
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.td_sink import TdUnavailable, TdVerifyError
from engine_core.guardian import limit_pool_d1 as d1

from test_limit_pool import DAY, make_deps, request

CFG = Path("/home/exedev/validation/operator-runs/taos-cfg")


@pytest.fixture(scope="module")
def td():
    if not os.environ.get("TD_CONFIG") and CFG.exists():
        os.environ["TD_CONFIG"] = str(CFG)
    try:
        import taos  # noqa: F401
        probe = PoolTaosStore("core_dev_c580_probe_" + str(os.getpid()))
        probe._cursor()
        probe.close()
    except (ImportError, TdUnavailable) as exc:
        pytest.skip("no reachable TDengine: " + str(exc)[:80])
    name = "core_dev_c580_" + str(os.getpid())
    store = PoolTaosStore(name)
    yield store
    store.drop_database()
    check = PoolTaosStore(name)
    assert check.schema_exists() is False
    check.close()
    store.close()


def _fresh(td):
    td.drop_database()
    td.ensure_schema()


def test_real_schema_has_every_contract_column_with_the_declared_type(td):
    _fresh(td)
    described = {row[0]: (row[1], row[3]) for row in td._run(f"DESCRIBE {td.database}.limit_up_pool_v1")}
    assert described["symbol"] == ("VARCHAR", "TAG") and described["ts"][0] == "TIMESTAMP"
    for name in DATA_COLUMNS:
        kind = lp.COLUMN_KIND[name]
        assert described[name][0] == {"int": "BIGINT", "float": "DOUBLE", "bool": "BOOL", "str": "VARCHAR"}[kind], name
    assert "market_data1" not in td.database


def test_real_kaipan_pool_roundtrips_every_cell_and_is_idempotent(td, tmp_path):
    _fresh(td)
    code, body = d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=td))
    assert code == 0 and body["detail"]["td_sync"]["status"] == "ok", body
    catalog = ArtifactCatalog(tmp_path)
    rows = catalog.read_rows(catalog.locate("limit_up_pool", DAY))
    mapped, _ = map_rows(rows)
    back = td.read_day_rows(DAY)
    assert set(back) == set(mapped) and len(back) == 55
    for symbol, want in mapped.items():
        for column in DATA_COLUMNS:
            have = back[symbol][column]
            assert (have == want[column]) or (isinstance(want[column], float) and abs(have - want[column]) < 1e-9), (symbol, column)
    # Chinese text survives (name, reason, plate text)
    sample = rows[0]
    assert back[sample["symbol"]]["name"] == sample["name"] and back[sample["symbol"]]["reason_plates_text"] == sample["reason_plates_text"]
    sink = LimitPoolTdSink(td, tmp_path)
    assert sink.sync_dates([DAY])[0].status == "unchanged"
    assert td.day_counts([DAY]) == {DAY: 55}
    versions = td.read_versions(DAY)
    assert len(versions) == 1 and versions[0]["business_status"] == "FORMAL" and versions[0]["quality"]["coverage"] == 1.0


def test_real_td_upgrade_replaces_the_day_and_keeps_both_versions(td, tmp_path):
    _fresh(td)
    deps = make_deps(store=td)
    assert d1.run_pool_tier(request("td"), tmp_path, deps=deps)[0] == 10
    assert td.day_counts([DAY]) == {DAY: 55} and td.read_versions(DAY)[0]["business_status"] == "DEGRADED"
    assert d1.run_pool_tier(request("kaipan"), tmp_path, deps=deps)[0] == 0
    versions = td.read_versions(DAY)
    assert {v["business_status"] for v in versions} == {"DEGRADED", "FORMAL"}
    kaipan_version = next(v for v in versions if v["business_status"] == "FORMAL")["version"]
    assert {row["version"] for row in td.read_day_rows(DAY).values()} == {kaipan_version}


def test_real_readback_detects_a_tampered_cell(td, tmp_path):
    _fresh(td)
    assert d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=td))[0] == 0
    catalog = ArtifactCatalog(tmp_path)
    mapped, _ = map_rows(catalog.read_rows(catalog.locate("limit_up_pool", DAY)))
    symbol = sorted(mapped)[0]
    version = td.read_day_rows(DAY)[symbol]["version"]
    tampered = {**mapped, symbol: {**mapped[symbol], "pct_chg": (mapped[symbol]["pct_chg"] or 0) + 1.0}}
    td.replace_day(DAY, version, tampered)                       # what TD now holds differs from the catalog
    with pytest.raises(TdVerifyError):
        LimitPoolTdSink(td, tmp_path).verify_day(DAY, {"version": version}, mapped)
    td.replace_day(DAY, version, mapped)                         # restored: verification passes again
    assert LimitPoolTdSink(td, tmp_path).verify_day(DAY, {"version": version}, mapped)["rows"] == len(mapped)
