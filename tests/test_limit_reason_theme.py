import json
from pathlib import Path

from engine_core.limit_reason_theme import (
    build_limit_reason_themes,
    load_kaipan_theme_snapshot,
)
from engine_core.ladder_cluster import build_ladder_clusters


SAMPLE = Path("/home/exedev/validation/kaipan-daily/2026-10-16-085002")


def test_1016_snapshot_uses_pool_theme_labels_before_stale_static_membership():
    result = load_kaipan_theme_snapshot(
        SAMPLE.parent, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:26:00+08:00",
        plate_names={"803038": "冰雪产业", "801267": "乳业", "801827": "AI视频",
                     "801801": "互动影游", "803023": "AI应用"},
        static_themes_by_symbol={"000530": ["外卖概念"], "001318": ["食品饮料"],
                                 "002343": ["影视"]},
    )

    ice = result["assignments"]["000530"]
    milk = result["assignments"]["001318"]
    cwen = result["assignments"]["002343"]
    assert (ice["theme_key"], ice["theme_name"], ice["theme_source"]) == (
        "803038", "冰雪产业", "POOL_LABEL")
    assert "算力" in [theme["name"] for theme in ice["secondary_themes"]]
    assert milk["theme_source"] == "POOL_LABEL"
    assert cwen["theme_source"] == "POOL_LABEL"
    assert cwen["theme_name"] == "AI应用"
    ladder = build_ladder_clusters(
        [
            {"symbol": "000530", "name": "冰山冷热", "streak": 1, "0925": 4.2},
            {"symbol": "001318", "name": "阳光乳业", "streak": 1, "0925": 2.1},
        ],
        [{"symbol": "000530", "plate": "外卖概念"}, {"symbol": "001318", "plate": "食品饮料"}],
        {"外卖概念": {"class": "THEME", "size": 297},
         "食品饮料": {"class": "THEME", "size": 100}},
        stage="auction", theme_assignments=result["assignments"],
        theme_basis=result["basis"],
    )
    clusters = ladder["clusters"]
    assert {row["name"] for row in clusters} == {"冰雪产业", "食品饮料"}
    clustered_symbols = [path["symbol"] for cluster in clusters for path in cluster["paths"]]
    assert clustered_symbols.count("000530") == 1
    assert ladder["attribution_basis"].startswith("昨日涨停池题材/历史原因；来源计数 ")


def test_reason_latest_and_history_are_as_of_previous_session():
    pool = {"info": [[["000001", "甲", 0, "", 0, "", 0, 0, 0, 0, 0, 0, "", 0, 0, 1, 0, 0, "", ""]]]}
    reasons = {"000001": {"StockID": "000001", "List": [
        {"Date": "2026-10-14", "Type": "0", "ZSCode": ["801001"],
         "Reason": "历史题材；长描述", "SCLT": "日内龙二"},
        {"Date": "2026-10-15", "Type": "0", "ZSCode": ["801002", "801003"],
         "Reason": "最新题材；更多内容", "SCLT": "日内龙一"},
        {"Date": "2026-10-16", "Type": "0", "ZSCode": ["801999"], "Reason": "未来原因"},
    ]}}
    out = build_limit_reason_themes(
        pool, reasons, expected_previous_date="2026-10-15",
        plate_names={"801001": "历史题材", "801002": "最新题材", "801003": "并列题材"},
        static_themes_by_symbol={},
    )["000001"]
    assert out["theme_key"] == "801002"
    assert out["theme_source"] == "REASON_LATEST"
    assert out["reason_text_short"] == "最新题材"
    assert out["tier_label"] == "日内龙一"
    assert [row["code"] for row in out["secondary_themes"]] == ["801003"]


def test_trading_session_window_is_built_once_for_all_symbols(monkeypatch):
    import engine_core.limit_reason_theme as themes

    original = themes.re.fullmatch
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(themes.re, "fullmatch", counted)
    symbols = {f"{index:06d}": {"List": []} for index in range(20)}
    out = build_limit_reason_themes(
        {}, symbols, expected_previous_date="2026-10-15", plate_names={},
        trading_dates=["2026-10-14", "2026-10-15", "bad-date"])

    assert len(out) == 20
    assert calls == 3


def test_history_frequency_and_static_fallback_are_explicit():
    reasons = {"000002": {"StockID": "000002", "List": [
        {"Date": "2026-10-15", "Type": "0", "ZSCode": [], "Reason": "暂无题材"},
        {"Date": "2026-10-14", "Type": "0", "ZSCode": ["801002"], "Reason": "乙题材"},
        {"Date": "2026-10-13", "Type": "0", "ZSCode": ["801002"], "Reason": "乙题材"},
    ]}}
    pool = {"info": [[["000002", "乙", 0, "", 0, "", 0, 0, 0, 0, 0, 0, "", 0, 0, 2, 0, 0, "", ""]]]}
    historic = build_limit_reason_themes(
        pool, reasons, expected_previous_date="2026-10-15",
        plate_names={"801001": "甲题材", "801002": "乙题材"}, static_themes_by_symbol={})["000002"]
    assert historic["theme_source"] == "NONE"
    assert historic["history_status"] == "CALENDAR_MISSING"

    no_reason = build_limit_reason_themes(
        {"info": [[["000003", "丙", 0, "", 0, "", 0, 0, 0, 0, 0, 0, "", 0, 0, 1, 0, 0, "", ""]]]},
        {}, expected_previous_date="2026-10-15", plate_names={},
        static_themes_by_symbol={"000003": [{"code": "801010", "name": "静态主题", "class": "THEME", "size": 25}]})["000003"]
    assert no_reason["theme_source"] == "STATIC_FALLBACK"
    assert no_reason["theme_name"] == "静态主题"

    unverified_static = build_limit_reason_themes(
        {}, {}, expected_previous_date="2026-10-15", plate_names={},
        static_themes_by_symbol={"000005": ["未验证规模的题材"]})["000005"]
    assert unverified_static["theme_source"] == "NONE"


def test_history_frequency_uses_all_records_in_twenty_sessions():
    dates = [f"2026-09-{day:02d}" for day in range(1, 21)]
    # 21 older rows on a valid session must not hide the 20 newer rows.
    reason_rows = [{"Date": dates[0], "Type": "0", "ZSCode": ["801001"]}
                   for _ in range(21)]
    reason_rows.extend({"Date": day, "Type": "0",
                        "ZSCode": ["801002"] if day == dates[-2] else []}
                       for day in dates[-2:]
                       for _ in range(10))
    result = build_limit_reason_themes(
        {}, {"000010": {"StockID": "000010", "List": reason_rows[:-1] + [reason_rows[-1]]}},
        expected_previous_date="2026-09-21", plate_names={"801001": "甲", "801002": "乙"},
        trading_dates=dates)
    assert result["000010"]["theme_key"] == "801001"


def test_missing_calendar_marks_reason_window_degraded_and_excludes_old_history():
    result = build_limit_reason_themes(
        {}, {"000011": {"StockID": "000011", "List": [
            {"Date": "2020-01-02", "Type": "0", "ZSCode": ["801001"], "Reason": "旧原因"},
        ]}}, expected_previous_date="2026-10-16", plate_names={"801001": "旧题材"})
    assert result["000011"]["theme_source"] == "NONE"
    assert result["000011"]["history_status"] == "CALENDAR_MISSING"


def test_naive_reason_timestamp_is_shanghai_local():
    from engine_core.limit_reason_theme import _parse_time
    parsed = _parse_time("2026-10-16T09:00:00")
    assert parsed.isoformat() == "2026-10-16T09:00:00+08:00"


def test_missing_and_stale_daily_snapshots_degrade_without_using_future_data(tmp_path):
    missing = load_kaipan_theme_snapshot(
        tmp_path, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:26:00+08:00", plate_names={},
        static_themes_by_symbol={"000004": [{"name": "登记表题材", "class": "THEME", "size": 12}]})
    assert missing["status"] == "MISSING"
    assert missing["basis"].startswith("登记表回退；来源计数 ")

    dated = tmp_path / "2026-10-16-085000"
    (dated / "pools").mkdir(parents=True)
    (dated / "reasons").mkdir()
    (dated / "manifest.json").write_text(json.dumps({"ended_at_utc": "2026-10-16T00:51:00+00:00"}), encoding="utf-8")
    (dated / "pools" / "2026-10-14_ban1.json").write_text('{"info":[]}', encoding="utf-8")
    stale = load_kaipan_theme_snapshot(
        tmp_path, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:26:00+08:00", plate_names={},
        static_themes_by_symbol={"000004": [{"name": "登记表题材", "class": "THEME", "size": 12}]})
    assert stale["status"] == "STALE"
    assert stale["basis"].startswith("登记表回退；来源计数 ")


def test_snapshot_after_report_cutoff_is_not_loaded(tmp_path):
    prior = tmp_path / "2026-10-16-092000"
    later = tmp_path / "2026-10-16-095000"
    for folder, ended, symbol, hot_name in (
        (prior, "2026-10-16T01:20:00+00:00", "000001", "盘前热板"),
        (later, "2026-10-16T01:50:00+00:00", "000002", "盘后热板"),
    ):
        (folder / "pools").mkdir(parents=True)
        (folder / "reasons").mkdir()
        (folder / "hot_plates").mkdir()
        (folder / "manifest.json").write_text(json.dumps({
            "ended_at_utc": ended, "pool_complete_by_date": {"2026-10-15": True}}), encoding="utf-8")
        for level in range(1, 6):
            (folder / "pools" / f"2026-10-15_ban{level}.json").write_text(json.dumps({
                "info": [[{"symbol": symbol, "plate": "昨日题材", "tier_label": "首板"}]]}), encoding="utf-8")
        (folder / "hot_plates" / "2026-10-16.json").write_text(json.dumps({
            "list": [["801001", hot_name]]}), encoding="utf-8")

    result = load_kaipan_theme_snapshot(
        tmp_path, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:45:00+08:00", plate_names={"801001": "主题"})
    assert result["snapshot_dir"] == str(prior)
    assert "000001" in result["assignments"]
    assert "000002" not in result["assignments"]
    assert result["hot_plate_names"] == ["盘前热板"]


def test_imported_pool_fields_and_reason_fields_survive_and_recent_hot_sample_is_selected():
    from engine_core.limit_reason_theme import load_kaipan_theme_snapshot
    from examples.import_reference_to_core_store import _rows

    fixture = Path(__file__).parent / "fixtures" / "kaipan-daily"
    pool_rows = _rows(fixture, "limit_up_pool")
    reason_rows = _rows(fixture, "limit_reasons")
    imported_hot = _rows(fixture, "hot_plates")
    # The importer accepts the actual daily directory shape; one row is enough
    # to assert that positional pool metadata and the independent reason columns
    # remain queryable after flattening.
    assert pool_rows
    assert all(key in pool_rows[0] for key in ("plate_label", "theme_text", "theme_codes"))
    assert all(key in reason_rows[0] for key in ("ZSCode", "PZSCode", "Reason", "Date", "SCLT"))
    assert imported_hot and imported_hot[0].get("available_at")

    rows = [
        {"code": "801001", "name": "旧热板", "available_at": "2026-10-16T09:20:00+08:00"},
        {"code": "801001", "name": "新热板", "available_at": "2026-10-16T09:25:00+08:00"},
        {"code": "801002", "name": "未来热板", "available_at": "2026-10-16T09:27:00+08:00"},
    ]
    result = load_kaipan_theme_snapshot(
        None, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:26:00+08:00", plate_names={}, hot_rows=rows)
    assert result["hot_plate_names"] == ["新热板"]
    assert result["hot_plate_samples"]["801001"]["sampled_at"] == "2026-10-16T09:25:00+08:00"


def test_reason_history_uses_last_twenty_trade_dates_and_reports_source_counts():
    dates = [f"2026-10-{day:02d}" for day in range(1, 22)]
    records = [{"Date": date, "Type": "0", "ZSCode": ["801001" if date in dates[-2:] else "801002"]}
               for date in dates]
    result = load_kaipan_theme_snapshot(
        None, report_date="2026-10-22", previous_trade_date=dates[-1], observed_at=None,
        plate_names={"801001": "近因", "801002": "旧因"},
        reason_rows=[{"symbol": "000001", **row} for row in records],
        trading_dates=dates)
    assert result["assignments"]["000001"]["theme_name"] == "近因"
    assert result["source_counts"]["REASON_LATEST"] == 1


def test_cached_cold_theme_snapshot_plus_runtime_static_matches_direct_snapshot():
    from engine_core.limit_reason_theme import merge_theme_snapshot_static_fallback

    pool = [{"symbol": "000001", "plate": "昨日题材"}]
    reasons = [
        {"symbol": "000001", "Date": "2026-10-15", "Type": "0",
         "ZSCode": ["801001"], "Reason": "昨日原因"},
        {"symbol": "000002", "Date": "2026-10-14", "Type": "0",
         "ZSCode": [], "Reason": "无代码原因"},
        {"symbol": "000003", "Date": "2026-10-15", "Type": "0",
         "ZSCode": ["801001"], "Reason": "代码映射原因"},
    ]
    static = {
        "000001": [{"name": "静态甲", "class": "THEME", "size": 20}],
        "000002": [{"name": "静态乙", "class": "THEME", "size": 25}],
        "000003": [{"name": "静态丙", "class": "NICHE", "size": 12}],
    }
    common = dict(report_date="2026-10-16", previous_trade_date="2026-10-15",
                  observed_at="2026-10-16T09:26:00+08:00",
                  plate_names={"801001": "原因题材"}, pool_rows=pool,
                  reason_rows=reasons, trading_dates=["2026-10-14", "2026-10-15"])
    cold = load_kaipan_theme_snapshot(
        None, **{**common, "plate_names": {"801001": "旧版名称"}})
    merged = merge_theme_snapshot_static_fallback(
        cold, static, expected_previous_date="2026-10-15",
        plate_names=common["plate_names"], trading_dates=common["trading_dates"])
    direct = load_kaipan_theme_snapshot(
        None, **common, static_themes_by_symbol=static)

    assert merged == direct
