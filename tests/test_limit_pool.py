"""C580: D-1 limit-up pool - pure core, tiers, ladder config, contract checker, TD sink (fake store), mutations.

All fixtures are recorded REAL responses (tests/fixtures/limit_pool/PROVENANCE.json); no hand-written pool rows.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline import limit_pool as lp
from engine_core.data_pipeline.dataset_contract import check_partition
from engine_core.data_pipeline.limit_pool import PoolSourceError
from engine_core.data_pipeline.limit_pool_td import LimitPoolTdSink, MemoryPoolStore, map_rows
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.td_sink import TdVerifyError
from engine_core.guardian import limit_pool_d1 as d1

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "limit_pool"
DAY, DAY2 = "2026-10-26", "2026-10-23"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def kaipan_pages(day=DAY):
    return {int(k): v for k, v in _load(f"kaipan_pool_{day.replace('-', '')}.json")["pages"].items()}


def wencai_doc(day=DAY):
    return _load(f"wencai_pool_{day.replace('-', '')}.json")["response"]


def td_inputs(day=DAY):
    doc = _load(f"td_bars_{day.replace('-', '')}.json")
    bars = {s: [tuple(b) for b in series] for s, series in doc["bars"].items()}
    return bars, doc["names"], doc["sessions"]


def schedule():
    return json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))


def pool_event_and_ladder(sched=None):
    sched = sched or schedule()
    event = next(e for e in sched["events"] if e["event_id"] == d1.EVENT_ID)
    return event, sched["profiles"][event["profile"]]["ladder"]


# ---- board limits ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("symbol,expected", [
    ("600000", 10.0), ("000001", 10.0), ("002724", 10.0), ("688981", 20.0), ("689009", 20.0), ("300750", 20.0),
    ("301660", 20.0), ("830799", 30.0), ("430047", 30.0), ("920091", 30.0), ("603278", 10.0)])
def test_limit_pct_depends_on_board_only(symbol, expected):
    assert lp.limit_pct(symbol) == expected


def test_there_is_no_st_five_percent_tier():
    # ST names share the ordinary board limit (FACTS 2026-10-13): 5% must never come back
    assert 5.0 not in {lp.limit_pct(s) for s in ("600000", "000001", "688981", "300750", "830799", "920091")}
    assert lp.is_st_name("*ST威领") and lp.is_st_name("ST仕净") and not lp.is_st_name("万向钱潮")


# ---- tier 1: Kaipan ------------------------------------------------------------------------------------------------
def test_kaipan_keeps_every_positional_field():
    part = lp.build_kaipan(DAY, kaipan_pages())
    assert len(part.rows) == 55 and part.manifest["provider"] == lp.KAIPAN
    row = next(r for r in part.rows if r["symbol"] == "000058") if any(r["symbol"] == "000058" for r in part.rows) else part.rows[0]
    raw = next(rec for page in kaipan_pages().values() for rec in page["records"] if rec[0] == row["symbol"])
    assert len(raw) == lp.KAIPAN_FULL_FIELDS
    for position, column in lp.KAIPAN_POSITIONS.items():
        value = raw[position]
        got = row[column]
        if value in ("", None):
            assert got is None, column
        else:
            assert got == (value if isinstance(value, str) else pytest.approx(float(value))), column
    assert row["name"] == raw[1] and row["lb_days"] == raw[15]  # level == vendor [15] on this day
    assert all(r["date"] == r["trade_date"] == DAY for r in part.rows)
    q = part.manifest["quality"]
    assert q["expected_basis"] == "pool_members" and q["expected_count"] == 55 and q["missing"] == 0
    assert q["coverage"] == 1.0 and q["partial"] is False and q["dropped_duplicates"] == 0
    assert part.manifest["source_dates"]["values"] == [DAY]


def test_kaipan_lb_days_is_the_record_field_15_not_the_page_level():
    # real 2026-10-23: 002724 and 603278 sit on ladder level 5 but their own [15] says 6 (six real limit-up closes)
    part = lp.build_kaipan(DAY2, kaipan_pages(DAY2))
    by_symbol = {r["symbol"]: r for r in part.rows}
    assert by_symbol["002724"]["lb_days"] == 6 and by_symbol["603278"]["lb_days"] == 6
    assert part.manifest["provider_details"]["lb_days_differs_from_ladder_level"] == 2
    clean = lp.build_kaipan(DAY, kaipan_pages())
    assert clean.manifest["provider_details"]["lb_days_differs_from_ladder_level"] == 0
    # empty [15] falls back to the level the record came from
    pages = copy.deepcopy(kaipan_pages())
    pages[2]["records"][0][15] = ""
    assert next(r for r in lp.build_kaipan(DAY, pages).rows if r["symbol"] == pages[2]["records"][0][0])["lb_days"] == 2


def test_kaipan_date_echo_mismatch_is_rejected():
    pages = copy.deepcopy(kaipan_pages())
    pages[2]["date_echo"] = "2026-10-25"
    with pytest.raises(PoolSourceError) as err:
        lp.build_kaipan(DAY, pages)
    assert err.value.code == "TIER_DATE_MISMATCH" and err.value.exit_code == 40 and not err.value.retryable


def test_kaipan_missing_date_echo_is_rejected():
    pages = copy.deepcopy(kaipan_pages())
    pages[1]["date_echo"] = None
    with pytest.raises(PoolSourceError) as err:
        lp.build_kaipan(DAY, pages)
    assert err.value.code == "TIER_DATE_MISMATCH"


def test_kaipan_short_records_are_dropped_and_counted_not_trusted():
    pages = copy.deepcopy(kaipan_pages())
    pages[1]["records"].append(["999999", "短", 0, ""])       # noise: far fewer than 16 fields
    part = lp.build_kaipan(DAY, pages)
    assert len(part.rows) == 55
    q = part.manifest["quality"]
    assert q["dropped_short_records"] == 1 and q["expected_count"] == 56 and q["missing"] == 1 and q["partial"] is True


def test_kaipan_level_one_empty_means_not_ready_retryable():
    with pytest.raises(PoolSourceError) as err:
        lp.build_kaipan(DAY, {})
    assert err.value.code == "KAIPAN_NOT_READY" and err.value.exit_code == 30 and err.value.retryable


def test_real_not_ready_response_yields_empty_pages_from_the_provider():
    # real answer of 2026-10-28 ~10:00 for the not yet published day: {'errcode': '1020', 'errmsg': '参数出错'}
    not_ready = _load("his_bans_not_ready_20261028.json")["response"]

    class Client:
        def get_his_bans(self, day, level):
            return not_ready

    result = d1.fetch_kaipan_pages("2026-10-28", {"kaipan_min_interval_seconds": 0.2}, client=Client())
    assert result["pages"] == {}
    with pytest.raises(PoolSourceError) as err:
        lp.build_kaipan("2026-10-28", result["pages"])
    assert err.value.code == "KAIPAN_NOT_READY"


def test_provider_walks_levels_until_errcode_1020_and_keeps_raw_records():
    raw = _load("his_bans_20261027_l1_l2.json")["levels"]
    top = {"errcode": "1020", "errmsg": "参数出错"}

    class Client:
        calls = []

        def get_his_bans(self, day, level):
            self.calls.append(level)
            return raw.get(str(level), top)

    client = Client()
    result = d1.fetch_kaipan_pages("2026-10-27", {"kaipan_min_interval_seconds": 0.2}, client=client)
    assert client.calls == [1, 2, 3] and sorted(result["pages"]) == [1, 2]
    assert all(len(rec) == 23 for page in result["pages"].values() for rec in page["records"])
    assert {p["date_echo"] for p in result["pages"].values()} == {"2026-10-27"}


# ---- tier 2: WenCai ------------------------------------------------------------------------------------------------
def test_wencai_members_and_streaks_match_kaipan_on_both_days():
    for day in (DAY, DAY2):
        doc = wencai_doc(day)
        wc = lp.build_wencai(day, doc["data"], reported_total=doc["row_count"])
        kp = lp.build_kaipan(day, kaipan_pages(day))
        assert {r["symbol"] for r in wc.rows} == {r["symbol"] for r in kp.rows}
        assert {r["symbol"]: r["lb_days"] for r in wc.rows} == {r["symbol"]: r["lb_days"] for r in kp.rows}
        assert wc.manifest["provider"] == lp.WENCAI and wc.manifest["business_status"] == "PROVISIONAL"
        assert wc.manifest["degraded"] is True and wc.manifest["quality"]["expected_count"] == len(doc["data"])
        assert wc.manifest["source_dates"] == {"kind": "wencai_column_suffix", "values": [day]}


def test_wencai_extra_columns_are_kept_with_units():
    doc = wencai_doc()
    part = lp.build_wencai(DAY, doc["data"], reported_total=doc["row_count"])
    row = next(r for r in part.rows if r["symbol"] == "000411")
    src = next(r for r in doc["data"] if r["code"] == "000411")
    assert row["wc_seal_amount_yuan"] == pytest.approx(src[f"涨停封单额[{DAY.replace('-', '')}]"])
    assert row["wc_open_count"] == src[f"涨停开板次数[{DAY.replace('-', '')}]"]
    assert row["wc_reason_category"] == src[f"涨停原因类别[{DAY.replace('-', '')}]"]
    assert part.manifest["field_units"]["wc_seal_amount_yuan"] == "yuan"


def test_wencai_wrong_date_columns_are_rejected():
    doc = wencai_doc()
    with pytest.raises(PoolSourceError) as err:
        lp.build_wencai("2026-10-27", doc["data"], reported_total=doc["row_count"])
    assert err.value.code == "TIER_DATE_MISMATCH"


def test_wencai_without_dated_columns_is_rejected():
    stripped = [{k.split("[")[0]: v for k, v in rec.items()} for rec in wencai_doc()["data"]]
    with pytest.raises(PoolSourceError) as err:
        lp.build_wencai(DAY, stripped, reported_total=len(stripped))
    assert err.value.code == "TIER_DATE_MISMATCH"


def test_wencai_empty_is_no_data():
    with pytest.raises(PoolSourceError) as err:
        lp.build_wencai(DAY, [], reported_total=0)
    assert err.value.code == "TIER_NO_DATA" and err.value.exit_code == 40


# ---- tier 3: TD derived --------------------------------------------------------------------------------------------
def test_td_derived_equals_kaipan_members_and_streaks_on_the_clean_day():
    bars, names, sessions = td_inputs()
    part = lp.build_td_derived(DAY, bars, names=names, sessions=sessions, st_source="fixture")
    kp = lp.build_kaipan(DAY, kaipan_pages())
    assert [r["symbol"] for r in part.rows] == [r["symbol"] for r in kp.rows]
    assert {r["symbol"]: r["lb_days"] for r in part.rows} == {r["symbol"]: r["lb_days"] for r in kp.rows}
    m = part.manifest
    assert m["business_status"] == "DEGRADED" and m["degraded"] is True and m["provider"] == lp.TD_DERIVED
    assert m["provider_details"]["derived"] is True and m["provider_details"]["st_filter"] == "EXCLUDED_BY_NAME"
    assert m["provider_details"]["st_excluded"] == 4


def test_td_derived_without_names_marks_st_filter_unavailable_and_does_not_guess():
    bars, _names, sessions = td_inputs()
    part = lp.build_td_derived(DAY, bars, names=None, sessions=sessions)
    assert part.manifest["provider_details"]["st_filter"] == "UNAVAILABLE"
    assert part.manifest["quality"]["st_filter"] == "UNAVAILABLE"
    assert len(part.rows) == 59        # 55 + 4 ST members that cannot be told apart without a name list


def test_td_derived_requires_a_bar_for_the_date():
    bars, names, sessions = td_inputs()
    with pytest.raises(PoolSourceError) as err:
        lp.build_td_derived("2026-10-28", bars, names=names, sessions=sessions)
    assert err.value.code == "TIER_NO_DATA"


def test_td_rule_is_close_equals_high_and_pct_threshold():
    ok = ("2026-10-26", 10.0, 10.0, 9.4)          # 10% board, 9.4 >= 10 - 0.6
    assert lp.bar_is_limit_up("600000", ok)
    assert not lp.bar_is_limit_up("600000", ("2026-10-26", 10.0, 10.0, 9.39))
    assert not lp.bar_is_limit_up("600000", ("2026-10-26", 9.9, 10.0, 9.9))        # close != high
    assert not lp.bar_is_limit_up("688981", ("2026-10-26", 10.0, 10.0, 10.0))      # 20% board needs >= 19.4
    assert lp.bar_is_limit_up("688981", ("2026-10-26", 10.0, 10.0, 19.9))
    assert not lp.bar_is_limit_up("600000", None)


# ---- tier runner: catalog + contract checker + ordering ------------------------------------------------------------
def make_deps(*, kaipan=True, wencai=True, td=True, store=None):
    def kaipan_fn(day, args):
        if not kaipan:
            raise PoolSourceError("KAIPAN_NOT_READY", "level_1_empty", exit_code=30, retryable=True)
        return {"pages": kaipan_pages(day), "request_count": 6}

    def wencai_fn(day, **kwargs):
        if not wencai:
            raise PoolSourceError("TIER_PARTIAL", "pages_truncated", exit_code=30, retryable=True)
        doc = wencai_doc(day)
        return {"records": doc["data"], "reported_total": doc["row_count"], "request_count": 1, "cookie_status": "used"}

    def bars_fn(day):
        return td_inputs(day)[0]

    def universe_fn(root, day):
        return td_inputs(day)[1], "fixture"

    return d1.PoolDeps(kaipan=kaipan_fn, wencai=wencai_fn, td_bars=bars_fn, universe=universe_fn,
                       td_store=(lambda: store) if store is not None else (lambda: MemoryPoolStore()))


def request(source, day=DAY, now="2026-10-27T01:00:00+08:00", **extra):
    args = {"dataset": "limit_up_pool", "source": source, "chain": f"{lp.KAIPAN},{lp.WENCAI},{lp.TD_DERIVED}", **extra}
    return {"data_date": day, "args": args, "now": now, "tier": source}


def test_kaipan_rung_commits_formal_partition_that_passes_the_contract_checker(tmp_path):
    store = MemoryPoolStore()
    code, body = d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=store))
    assert code == 0 and body["status"] == "ok", body
    assert body["detail"]["td_sync"]["status"] == "ok" and store.days[DAY]
    report = check_partition(tmp_path, "limit_up_pool", DAY)
    assert report.passed, report.summary()
    manifest = json.loads(ArtifactCatalog(tmp_path).locate("limit_up_pool", DAY).read_text(encoding="utf-8"))
    assert manifest["provider"] == lp.KAIPAN == manifest["source"] and manifest["business_status"] == "FORMAL"
    assert manifest["provider_chain"] == [lp.KAIPAN, lp.WENCAI, lp.TD_DERIVED] and manifest["degraded"] is False
    assert manifest["as_of"] == f"{DAY}T15:00:00+08:00" and manifest["fetched_at"] and manifest["available_at"]


def test_wencai_and_td_rungs_pass_the_checker_and_are_degraded(tmp_path):
    for source, status, provider in (("wencai", "PROVISIONAL", lp.WENCAI), ("td", "DEGRADED", lp.TD_DERIVED)):
        root = tmp_path / source
        code, body = d1.run_pool_tier(request(source), root, deps=make_deps())
        assert code == 10 and body["status"] == "degraded", body
        report = check_partition(root, "limit_up_pool", DAY)
        assert report.passed, report.summary()
        manifest = json.loads(ArtifactCatalog(root).locate("limit_up_pool", DAY).read_text(encoding="utf-8"))
        assert manifest["business_status"] == status and manifest["provider"] == provider and manifest["degraded"] is True


def test_equal_or_better_version_makes_lower_rungs_a_no_op(tmp_path):
    deps = make_deps()
    assert d1.run_pool_tier(request("kaipan"), tmp_path, deps=deps)[0] == 0
    for source in ("kaipan", "wencai", "td"):
        code, body = d1.run_pool_tier(request(source), tmp_path, deps=deps)
        assert code == 20 and body["error_code"] == "ALREADY_SATISFIED"
    versions = ArtifactCatalog(tmp_path).read()["datasets"]["limit_up_pool"]["dates"][DAY]
    assert len(versions) == 1


def test_late_better_version_wins_locate_and_checker(tmp_path):
    deps = make_deps()
    assert d1.run_pool_tier(request("td"), tmp_path, deps=deps)[0] == 10
    assert d1.run_pool_tier(request("wencai"), tmp_path, deps=deps)[0] == 10       # wencai (rank 1) beats td (rank 2)
    assert d1.run_pool_tier(request("kaipan"), tmp_path, deps=deps)[0] == 0
    catalog = ArtifactCatalog(tmp_path)
    best = json.loads(catalog.locate("limit_up_pool", DAY).read_text(encoding="utf-8"))
    assert best["provider"] == lp.KAIPAN
    assert check_partition(tmp_path, "limit_up_pool", DAY).passed
    # the reverse order never lowers: td after kaipan is a no-op (covered above); and locate prefers quality over recency
    assert len(catalog.read()["datasets"]["limit_up_pool"]["dates"][DAY]) == 3


def test_locate_and_latest_never_prefer_a_later_lower_quality_commit(tmp_path):
    """The runner refuses lower rungs, but the catalog itself must still protect the better version."""
    deps = make_deps()
    assert d1.run_pool_tier(request("kaipan"), tmp_path, deps=deps)[0] == 0
    bars, names, sessions = td_inputs()
    td = lp.build_td_derived(DAY, bars, names=names, sessions=sessions)
    manifest = {**td.manifest, "provider_chain": [lp.TD_DERIVED], "provider_attempts": [], "fetched_at": "2026-10-27T00:00:00Z",
                "as_of": f"{DAY}T15:00:00+08:00"}
    ArtifactCatalog(tmp_path).commit("limit_up_pool", DAY, td.rows, manifest, source=lp.TD_DERIVED)
    catalog = ArtifactCatalog(tmp_path)
    best = json.loads(catalog.locate("limit_up_pool", DAY).read_text(encoding="utf-8"))
    assert best["provider"] == lp.KAIPAN
    latest = catalog.read()["datasets"]["limit_up_pool"]["latest"]
    held = next(v for v in catalog.read()["datasets"]["limit_up_pool"]["dates"][DAY] if v["version"] == latest["version"])
    assert held["business_status"] == "FORMAL"
    assert check_partition(tmp_path, "limit_up_pool", DAY).passed


def test_not_ready_retry_until_converts_to_advance_only_after_the_configured_time(tmp_path):
    deps = make_deps(kaipan=False)
    early = d1.run_pool_tier(request("kaipan", now="2026-10-27T08:40:00+08:00", retry_until="08:50:00"), tmp_path, deps=deps)
    assert early[0] == 30 and early[1]["error_code"] == "KAIPAN_NOT_READY" and early[1]["retryable"] is True
    late = d1.run_pool_tier(request("kaipan", now="2026-10-27T08:55:00+08:00", retry_until="08:50:00"), tmp_path, deps=deps)
    assert late[0] == 40 and late[1]["error_code"] == "TIER_NO_DATA" and late[1]["retryable"] is False


def test_retryable_wencai_failure_is_exit_30_and_writes_nothing(tmp_path):
    code, body = d1.run_pool_tier(request("wencai"), tmp_path, deps=make_deps(wencai=False))
    assert code == 30 and body["retryable"] and body["error_code"] == "TIER_PARTIAL"
    assert ArtifactCatalog(tmp_path).locate("limit_up_pool", DAY) is None


def test_config_errors_are_exit_64(tmp_path):
    assert d1.run_pool_tier({"data_date": DAY, "args": {"source": "nope"}}, tmp_path)[0] == 64
    assert d1.run_pool_tier({"data_date": "", "args": {"source": "kaipan"}}, tmp_path)[0] == 64
    bad_chain = request("kaipan")
    bad_chain["args"]["chain"] = "wencai"
    assert d1.run_pool_tier(bad_chain, tmp_path)[0] == 64
    assert d1.run_pool_tier(request("kaipan", retry_until="8pm"), tmp_path)[0] == 64


def test_dry_run_fetches_nothing(tmp_path):
    def boom(*a, **k):
        raise AssertionError("dry run must not fetch")
    deps = d1.PoolDeps(kaipan=boom)
    code, body = d1.run_pool_tier(request("kaipan"), tmp_path, dry_run=True, deps=deps)
    assert code == 0 and body["status"] == "not_applicable"


def test_td_outage_does_not_fail_the_fetch_and_next_sync_catches_up(tmp_path):
    store = MemoryPoolStore()
    store.fail = "unavailable"
    code, body = d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=store))
    assert code == 0 and body["detail"]["td_sync"]["error_code"] == "TD_UNAVAILABLE"
    store.fail = None
    out = LimitPoolTdSink(store, tmp_path).sync_dates([DAY])[0]
    assert out.status == "synced" and store.days[DAY]
    assert LimitPoolTdSink(store, tmp_path).sync_dates([DAY])[0].status == "unchanged"


def test_ladder_pass_reads_rungs_from_schedule_and_stops_at_first_success(tmp_path):
    code, body = d1.run_ladder_once(DAY, tmp_path, deps=make_deps(kaipan=False))
    assert code == 10 and body["detail"]["partition"] == DAY
    tried = body["detail"]["ladder_pass"]
    assert [t["tier"] for t in tried] == ["kaipan", "wencai"] and tried[0]["error_code"] == "KAIPAN_NOT_READY"


def test_ladder_pass_all_fail_reports_missing_and_writes_nothing(tmp_path):
    deps = make_deps(kaipan=False, wencai=False)
    deps.td_bars = lambda day: {}
    code, body = d1.run_ladder_once(DAY, tmp_path, deps=deps)
    assert code == 40 and body["error_code"] == "TIER_NO_DATA"
    assert [t["tier"] for t in body["detail"]["ladder_pass"]] == ["kaipan", "wencai", "td_derived"]
    assert ArtifactCatalog(tmp_path).locate("limit_up_pool", DAY) is None


# ---- TD sink (fake store) ------------------------------------------------------------------------------------------
def test_td_sink_roundtrip_unchanged_and_lower_quality_is_skipped(tmp_path):
    store = MemoryPoolStore()
    deps = make_deps(store=store)
    d1.run_pool_tier(request("td"), tmp_path, deps=deps)
    sink = LimitPoolTdSink(store, tmp_path)
    meta = store.versions[DAY][0]
    assert meta["business_status"] == "DEGRADED" and meta["row_count"] == len(store.days[DAY])
    d1.run_pool_tier(request("kaipan"), tmp_path, deps=deps)              # upgrades TD to the Kaipan version
    assert {v["business_status"] for v in store.versions[DAY]} == {"DEGRADED", "FORMAL"}
    assert all(row["version"] == store.versions[DAY][-1]["version"] for row in store.days[DAY].values())
    assert sink.sync_dates([DAY])[0].status == "unchanged"
    # a lower version must not replace the better one already in TD
    store.versions[DAY] = [v for v in store.versions[DAY] if v["business_status"] == "FORMAL"]
    catalog = ArtifactCatalog(tmp_path).read()
    entries = catalog["datasets"]["limit_up_pool"]["dates"][DAY]
    catalog["datasets"]["limit_up_pool"]["dates"][DAY] = [e for e in entries if e["business_status"] == "DEGRADED"]
    assert sink.sync_date(DAY, catalog).status == "skipped_lower"


def test_td_mapping_matches_the_kaipan_row_values(tmp_path):
    store = MemoryPoolStore()
    d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=store))
    rows = ArtifactCatalog(tmp_path).read_rows(ArtifactCatalog(tmp_path).locate("limit_up_pool", DAY))
    mapped, dropped = map_rows(rows)
    assert dropped == 0 and set(mapped) == {r["symbol"] for r in rows}
    sample = rows[0]
    got = store.days[DAY][sample["symbol"]]
    assert got["name"] == sample["name"] and got["lb_days"] == sample["lb_days"]
    assert got["limit_time_epoch"] == sample["limit_time_epoch"] and got["reason_category"] == sample["reason_category"]
    assert got["pct_chg"] == pytest.approx(sample["pct_chg"]) and got["plate_code"] == sample["plate_code"]


@pytest.mark.parametrize("column", ["pct_chg", "reason_category", "lb_days", "limit_time_epoch"])
def test_td_readback_mismatch_is_caught(tmp_path, column):
    store = MemoryPoolStore()
    store.corrupt = column
    ArtifactCatalog(tmp_path)   # root only
    d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=MemoryPoolStore()))
    store = MemoryPoolStore()
    store.corrupt = column
    with pytest.raises(TdVerifyError):
        LimitPoolTdSink(store, tmp_path).sync_dates([DAY])


# ---- schedule configuration ----------------------------------------------------------------------------------------
def test_event_and_ladder_are_configured_as_designed():
    event, ladder = pool_event_and_ladder()
    assert event["at"] == "00:40:00" and event["data_date_offset"] == -1 and event["calendar"] == "trading_day"
    assert event["on_deadline"] == "missed" and event["late_until"] == event["deadline"] == "15:00:00"
    assert event["handler"] == "kaipan_fetch" and event["cost"] == "light"
    assert [rung["name"] for rung in ladder] == ["kaipan", "wencai", "td_derived"]
    assert [rung["args"]["source"] for rung in ladder] == ["kaipan", "wencai", "td"]
    chain = ladder[0]["args"]["chain"].split(",")
    assert chain == [lp.KAIPAN, lp.WENCAI, lp.TD_DERIVED]
    assert all(rung["args"]["chain"] == ladder[0]["args"]["chain"] for rung in ladder)
    assert ladder[0]["attempts"] == 34 and ladder[0]["backoff"] == "15m*1" and ladder[0]["args"]["retry_until"] == "08:50:00"
    assert ladder[1]["backoff"] == "30m*1" and ladder[1]["attempts"] == 2
    assert all(rung["args"]["dataset"] == "limit_up_pool" and "timeout" in rung for rung in ladder)
    # 00:40 + 33 x 15 min = 08:55: the last wait ends right after retry_until
    assert (ladder[0]["attempts"] - 1) * 15 + 40 == 8 * 60 + 55


def test_chain_order_in_config_matches_rung_order():
    _event, ladder = pool_event_and_ladder()
    by_source = {"kaipan": lp.KAIPAN, "wencai": lp.WENCAI, "td": lp.TD_DERIVED}
    assert [by_source[r["args"]["source"]] for r in ladder] == ladder[0]["args"]["chain"].split(",")


def test_registry_points_at_the_new_event_and_documents_td():
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text(encoding="utf-8"))
    item = next(i for i in registry["items"] if i["id"] == "limit_up_pool")
    assert item["guardian_event"] == "limit_pool_d1" == item["late_policy"]["trigger_event"]
    assert item["td_sink"]["super_table"] == "limit_up_pool_v1" and item["td_sink"]["database"] == "core_data"
    assert (ROOT / "native/guardiand/testdata/schedule.json").read_text() == (
        ROOT / "src/engine_core/guardian/schedule.json").read_text()


# ---- contract checker: mutations must fail -------------------------------------------------------------------------
def _committed(tmp_path, source="kaipan"):
    d1.run_pool_tier(request(source), tmp_path, deps=make_deps())
    manifest_path = ArtifactCatalog(tmp_path).locate("limit_up_pool", DAY)
    return manifest_path


def _rewrite(tmp_path, mutate_rows=None, mutate_manifest=None):
    """Re-commit a mutated copy through the catalog so only the targeted contract aspect is broken."""
    catalog = ArtifactCatalog(tmp_path)
    path = catalog.locate("limit_up_pool", DAY)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rows = catalog.read_rows(path)
    root = tmp_path / "mutant"
    for key in ("version", "sha256", "row_count", "storage_backend", "storage_uri", "rows_uri", "manifest_uri",
                "committed_at", "business_date", "storage_format"):
        manifest.pop(key, None)
    manifest["available_at"] = "2026-10-27T01:00:00+08:00"
    if mutate_rows:
        mutate_rows(rows)
    if mutate_manifest:
        mutate_manifest(manifest)
    ArtifactCatalog(root).commit("limit_up_pool", DAY, rows, manifest, source=manifest["provider"])
    return check_partition(root, "limit_up_pool", DAY)


def test_unmutated_copy_passes(tmp_path):
    _committed(tmp_path)
    assert _rewrite(tmp_path).passed


def test_mutation_wrong_unit_in_manifest_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_manifest=lambda m: m["field_units"].update(pct_chg="bp"))
    assert not report.passed and any(c["id"] == "A11" for c in report.to_dict()["violations"])


def test_mutation_dropped_column_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_rows=lambda rows: [r.pop("lb_days") for r in rows])
    assert not report.passed


def test_mutation_extra_unspecified_column_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_rows=lambda rows: rows[0].update(surprise=1))
    assert not report.passed and any(c["id"] == "B4" for c in report.to_dict()["violations"])


def test_mutation_row_date_is_the_fetch_day_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_rows=lambda rows: [r.update(date="2026-10-27") for r in rows])
    assert not report.passed and any(c["id"] == "B2" for c in report.to_dict()["violations"])


def test_mutation_manifest_field_removed_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_manifest=lambda m: m.pop("provider_attempts"))
    assert not report.passed and any(c["id"] == "A3" for c in report.to_dict()["violations"])


def test_mutation_denominator_changed_fails_checker(tmp_path):
    _committed(tmp_path)
    report = _rewrite(tmp_path, mutate_manifest=lambda m: m["quality"].update(expected_count=999))
    assert not report.passed and any(c["id"] == "C2" for c in report.to_dict()["violations"])


def test_mutation_kaipan_published_before_vendor_time_fails_checker(tmp_path):
    _committed(tmp_path)
    catalog = ArtifactCatalog(tmp_path)
    path = catalog.locate("limit_up_pool", DAY)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rows = catalog.read_rows(path)
    root = tmp_path / "early"
    for key in ("version", "sha256", "row_count", "storage_backend", "storage_uri", "rows_uri", "manifest_uri",
                "committed_at", "business_date", "storage_format"):
        manifest.pop(key, None)
    manifest["available_at"] = "2026-10-26T20:00:00+08:00"   # before D+1 00:30
    committed = ArtifactCatalog(root).commit("limit_up_pool", DAY, rows, manifest, source=manifest["provider"],
                                             clock=lambda: __import__("datetime").datetime(2026, 10, 26, 12, 0, tzinfo=__import__("datetime").timezone.utc))
    assert committed["available_at"].startswith("2026-10-26T12:00")
    report = check_partition(root, "limit_up_pool", DAY)
    assert not report.passed
    assert any(c["id"] in {"A10", "A10b"} for c in report.to_dict()["violations"])


# ---- mutations of the production logic: the tests above must notice --------------------------------------------------
def test_mutation_st_five_percent_tier_is_caught(monkeypatch):
    original = lp.limit_pct
    monkeypatch.setattr(lp, "limit_pct", lambda s: 5.0 if s == "600000" else original(s))
    assert lp.limit_pct("600000") == 5.0 and original("600000") == 10.0
    with pytest.raises(AssertionError):
        test_there_is_no_st_five_percent_tier()


def test_mutation_without_st_filter_adds_st_members(monkeypatch):
    bars, names, sessions = td_inputs()
    monkeypatch.setattr(lp, "is_st_name", lambda name: False)
    part = lp.build_td_derived(DAY, bars, names=names, sessions=sessions)
    assert len(part.rows) == 59 != 55   # test_td_derived_equals_kaipan_members... would fail on this


def test_mutation_no_date_evidence_would_accept_a_wrong_day():
    pages = copy.deepcopy(kaipan_pages())
    for page in pages.values():
        page["date_echo"] = "2026-10-24"
    with pytest.raises(PoolSourceError):
        lp.build_kaipan(DAY, pages)
    doc = wencai_doc()
    with pytest.raises(PoolSourceError):
        lp.build_wencai("2026-10-27", doc["data"], reported_total=doc["row_count"])


def test_td_sync_event_also_catches_up_the_limit_pool(tmp_path, monkeypatch):
    """The scheduled td_sync sweep copies pool partitions the fetch-time sync could not (TD was down)."""
    from engine_core.data_pipeline.td_sink import MemoryTdStore
    from engine_core.guardian.td_sync import run_td_sync
    from td_sink_support import commit_day

    monkeypatch.setenv("CORE_TD_DATABASE", "core_dev_c580_unit")
    down = MemoryPoolStore()
    down.fail = "unavailable"
    first = d1.run_pool_tier(request("kaipan"), tmp_path, deps=make_deps(store=down))
    assert first[0] == 0 and first[1]["detail"]["td_sync"]["status"] == "unavailable"
    commit_day(tmp_path, "2026-10-23", "baostock")         # the sweep needs at least one daily_kline day to run
    pool_store = MemoryPoolStore()
    code, body = run_td_sync({"trade_date": DAY, "args": {}, "now": "2026-10-26T18:00:00+08:00"}, tmp_path,
                             store=MemoryTdStore(), pool_store=pool_store)
    assert code == 0 and body["detail"]["limit_up_pool"]["status"] == "ok"
    assert body["detail"]["limit_up_pool"]["outcomes"][0]["status"] == "synced" and len(pool_store.days[DAY]) == 55
