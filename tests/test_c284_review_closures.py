import json
from datetime import datetime

from engine_core.auction_panorama import build_auction_panorama, normalize_hot_plates
from engine_core.auction_stock_facts import build_auction_stock_facts, store_auction_stock_facts
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.slices import SliceProviders, SliceReader


def test_missing_today_facts_does_not_relabel_yesterday(tmp_path):
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit(
        "auction_stock_facts", "2026-10-15",
        [{"symbol": "600001", "auction_amount_yuan": 123, "valid_price": True,
          "pct_change": 1, "seal": {"status": "UNKNOWN"}}],
        {"available_at": "2026-10-15T09:25:30+08:00", "as_of": "2026-10-15T09:25:06+08:00",
         "quality": {"universe_n": 1}}, source="fixture")
    result = SliceReader(SliceProviders(catalog=catalog, file_fallback=False).providers()).get(
        "auction_stock_facts", trade_date="2026-10-16", asof="2026-10-16T09:26:00+08:00")
    assert result.rows == []
    assert result.meta["status"] == "MISSING"


def test_top50_hot_view_preserves_whole_market_total(tmp_path, monkeypatch):
    import engine_core.auction_stock_facts as stock_facts_module
    from conftest import patch_catalog_with_declared_test_clock
    patch_catalog_with_declared_test_clock(monkeypatch, ArtifactCatalog)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-16T09:25:10+08:00")

    monkeypatch.setattr(stock_facts_module, "datetime", Clock)

    class Redis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, **kwargs):
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

    redis = Redis()
    snapshots = [{"symbol": f"600{i:03}", "name": str(i), "auction_amount_yuan": i + 1,
                  "price_milli": 10000, "pre_close_milli": 10000, "change_bp": i}
                 for i in range(60)]
    facts = build_auction_stock_facts("2026-10-16", "2026-10-16T09:25:06+08:00", snapshots, [],
                                      now="2026-10-16T09:25:10+08:00")
    store_auction_stock_facts(facts, artifact_root=tmp_path, redis=redis)

    def aggregate(client):
        result = SliceReader(SliceProviders(catalog=ArtifactCatalog(tmp_path), redis=client,
                                            file_fallback=False).providers()).get(
            "auction_stock_facts", trade_date="2026-10-16", asof="2026-10-16T09:26:00+08:00")
        product = build_auction_panorama(
            {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
             "rows": result.rows, "summary": result.meta["quality"]},
            plate_members=[], yesterday_pool=[])
        return product, len(result.rows)

    cold, cold_n = aggregate(None)
    hot, hot_n = aggregate(redis)
    assert hot["overview"] == cold["overview"]
    assert hot_n == cold_n == 60


def test_live_posthoc_environment_cannot_enable_future_hot_rank(monkeypatch):
    monkeypatch.setenv("POSTHOC_DEV", "1")
    result = normalize_hot_plates(
        [{"code": "p", "strength": 100}], fetched_at="2026-10-16T09:27:00+08:00",
        available_at="2026-10-16T09:27:00+08:00", asof="2026-10-16T09:26:00+08:00",
        trade_date="2026-10-16", now="2026-10-16T09:28:00+08:00")
    assert result["ranks"] == {}
    historical = normalize_hot_plates(
        [{"code": "p", "strength": 100}], fetched_at="2026-10-16T09:27:00+08:00",
        available_at="2026-10-16T09:27:00+08:00", asof="2026-10-16T09:26:00+08:00",
        trade_date="2026-10-16", now="2026-10-18T09:28:00+08:00")
    assert historical["basis"] == "POSTHOC_DEV" and historical["ranks"]


def test_actual_mail_preserves_v6_panorama_tables(tmp_path):
    from engine_core.email_render import render_email_html
    from engine_core.report_mailer import build_report_message

    facts = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
             "rows": [{"symbol": "600001", "name": "甲", "auction_amount_yuan": 100,
                       "pct_change": 1, "valid_price": True, "seal": {"status": "UNKNOWN"}}]}
    panorama = build_auction_panorama(facts, plate_members=[], yesterday_pool=[{"symbol": "600001", "lb_days": 2}])
    payload = {"trade_date": "2026-10-16", "stage": "auction_0926", "auction_panorama": panorama,
               "stocks": facts["rows"], "manifest": {"assembly": "v6"}, "chapters": {}}
    html, _ = render_email_html(payload, "auction", "2026-10-16")
    html_path, json_path = tmp_path / "report.html", tmp_path / "report.json"
    html_path.write_text(html, encoding="utf-8")
    persisted = dict(payload)
    persisted.pop("auction_panorama")
    json_path.write_text(json.dumps(persisted), encoding="utf-8")
    message = build_report_message("Core", "2026-10-16", "auction", html_path, json_path,
                                   ["audit@example.com"], "audit@localhost")
    mailed = message.get_body(preferencelist=("html",)).get_content()
    assert "竞价全景 V6" in mailed
    assert "①" in mailed and "②" in mailed


def test_auction_email_body_is_compact_panorama_without_attachment_by_default(tmp_path):
    import hashlib
    from engine_core import report_mailer
    from engine_core.report_mailer import build_report_message

    report_html = tmp_path / "report.html"
    report_json = tmp_path / "report.json"
    brief = {"email_lines": ["09:26 竞价速览 · 2026-10-16", "市场:普遍高开", "90.2%上涨",
                       "主看:光模块", "中位+2.16%", "备看:复合集流体", "中位+1.00%",
                       "风险:高标接力分化", "昨日≥2板竞价封下一板0/6", "封单极值:新华传媒17.65亿",
                       "全市场封单46.86亿", "结论:有板块观察候选", "阈值说明"],
              "lines": ["备看排序 diagnostic"]}
    full_html = "<html><body>竞价全景 V6 ① 板块竞价全景 ② 昨日连板梯队</body></html>"
    report_html.write_text(full_html, encoding="utf-8")
    panorama = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:26:00+08:00",
        "quickbrief": brief, "overview": {}, "plates": [], "yesterday_ladder": []}
    panorama_bytes = json.dumps(panorama, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    panorama_sidecar = {"trade_date": panorama["trade_date"], "asof": panorama["asof"],
        "version": "auction_panorama_v1", "sha256": hashlib.sha256(panorama_bytes).hexdigest(),
        "panorama": panorama}
    sidecar_bytes = json.dumps(panorama_sidecar, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (tmp_path / "auction_panorama.json").write_bytes(sidecar_bytes)
    report_json.write_text(json.dumps({"stage": "auction", "trade_date": panorama["trade_date"],
        "asof": panorama["asof"], "auction_quickbrief": brief,
        "auction_panorama_file": "auction_panorama.json",
        "auction_panorama_sha256": hashlib.sha256(sidecar_bytes).hexdigest()}), encoding="utf-8")

    message = build_report_message("Core", "2026-10-16", "auction", report_html, report_json,
        [report_mailer.ALLOWED_RECIPIENT], "reports@localhost")
    body = message.get_body(preferencelist=("plain",)).get_content()
    attachments = list(message.iter_attachments())
    assert "昨日整批统计" in body and "①题材横向对比" in body
    assert "附件" not in body
    assert "竞价全景 V6" not in body and "② 昨日连板梯队" not in body
    assert attachments == []
    assert "昨日整批统计" in body and "①题材横向对比" in body
    assert "②昨日涨停×题材接力意愿" in body
    assert len(body.encode("utf-8")) <= 30_000
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert len(html_body.encode("utf-8")) <= 30_000
    explicit = build_report_message("Core", "2026-10-16", "auction", report_html, report_json,
        [report_mailer.ALLOWED_RECIPIENT], "reports@localhost", attach_full_html=True)
    assert [part.get_filename() for part in explicit.iter_attachments()] == ["report.html"]
    from engine_core.email_render import render_email_html
    page, _ = render_email_html({"auction_panorama": panorama}, "auction", "2026-10-16",
        {"degraded": ["SAMPLE_DEGRADED"]},
        full_report=True)
    assert page.index("竞价全景 V6") < page.index("① 板块竞价全景")
    assert "备看排序 diagnostic" in page and "数据完整度与异常摘要" in page

    report_json.write_text(json.dumps({"stage": "auction"}), encoding="utf-8")
    fallback = build_report_message("Core", "2026-10-16", "auction", report_html, report_json,
        [report_mailer.ALLOWED_RECIPIENT], "reports@localhost")
    fallback_plain = fallback.get_body(preferencelist=("plain",)).get_content()
    fallback_html = fallback.get_body(preferencelist=("html",)).get_content()
    assert "竞价速览构建失败" in fallback_plain and 'role="alert"' in fallback_html
    assert "① 板块竞价全景" in fallback_html
    assert list(fallback.iter_attachments()) == []
