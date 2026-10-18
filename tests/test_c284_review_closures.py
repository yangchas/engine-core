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
