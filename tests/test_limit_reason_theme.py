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
    assert ladder["attribution_basis"] == "昨日涨停池题材/历史原因"


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
    assert historic["theme_source"] == "REASON_HISTORY"
    assert historic["theme_key"] == "801002"

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


def test_missing_and_stale_daily_snapshots_degrade_without_using_future_data(tmp_path):
    missing = load_kaipan_theme_snapshot(
        tmp_path, report_date="2026-10-16", previous_trade_date="2026-10-15",
        observed_at="2026-10-16T09:26:00+08:00", plate_names={},
        static_themes_by_symbol={"000004": [{"name": "登记表题材", "class": "THEME", "size": 12}]})
    assert missing["status"] == "MISSING"
    assert missing["basis"] == "登记表回退"

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
    assert stale["basis"] == "登记表回退"


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
