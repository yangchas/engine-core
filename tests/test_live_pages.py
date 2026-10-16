import json

from engine_core import live_pages
from examples import render_live_pages as compatibility_cli


def test_migrated_renderer_compatibility_json_matches_fixture_fields():
    td = type("FixtureTD", (), {"states_at": lambda self, cutoff: {}})()
    from_library_html, from_library = live_pages.render_page(td, trade_date="2026-10-15", stage="auction_0926")
    _, from_cli = compatibility_cli.render_page(td, trade_date="2026-10-15", stage="auction_0926")
    # Timings are observational and intentionally vary between invocations.
    for payload in (from_library, from_cli):
        payload["manifest"].pop("timings_ms", None)
    library_json = live_pages._json_report_payload(from_library)
    cli_json = compatibility_cli._json_report_payload(from_cli)
    assert json.loads(json.dumps(library_json, ensure_ascii=False, allow_nan=False)) == \
        json.loads(json.dumps(cli_json, ensure_ascii=False, allow_nan=False))
    assert compatibility_cli.render_page is live_pages.render_page
    assert payload["chapters"]["extremes_board"]["auction"]["selected_count"] == 0
    assert payload["chapters"]["market_price_volume"]["status"] == "STALE"
    assert payload["chapters"]["market_price_volume"]["market_match_amount_yuan"] is None
    assert payload["chapters"]["market_price_volume"]["symbol_count"] is None
    assert payload["chapters"]["market_price_volume"]["priced_symbol_count"] is None
    assert payload["snapshots"][-1]["market_match_amount_yuan"] is None
    assert "竞价先选极值集合" in from_library_html


def test_live_report_json_preserves_a_to_b_v2_contract_fields():
    report = {"trade_date": "2026-10-15", "stocks": [],
              "a_to_b_schema_version": 2,
              "a_to_b": [{"schema_version": 2, "a_name": "锚点",
                          "a_strength": {"auction_amount_rank": 1},
                          "a_reasons": [{"rule_id": "R1", "text": "昨日≥2板"}],
                          "plate_state": {"name": "芯片"},
                          "sentiment_state": {"label": "WEAK"},
                          "b_candidates": [{"name": "同伴", "reasons": ["板块竞价额第1"],
                                            "metrics": {"auction_pct": 1}, "buyable": True}]}]}
    payload = live_pages._json_report_payload(report)
    assert payload["a_to_b_schema_version"] == 2
    assert payload["a_to_b"][0]["a_strength"]["auction_amount_rank"] == 1
    assert payload["a_to_b"][0]["b_candidates"][0]["name"] == "同伴"


def test_kaipan_missing_net_is_not_filled_as_zero(tmp_path):
    from datetime import datetime

    hot = tmp_path / "hot.json"
    hot.write_text(json.dumps({"captured_local": "2026-10-16T09:26:00+08:00",
                               "response": {"list": [["801001", "通信", 10, 1.2, None,
                                                        3_000_000_000, None, None, None]]}}),
                   encoding="utf-8")
    result = live_pages._plate_quadrants(
        {"top_plates_by_share": [{"name": "通信", "code": "801001", "equal_weight_pct": 1.0,
                                  "member_count": 20}]}, {}, {}, str(hot), None,
        datetime.fromisoformat("2026-10-16T09:27:00+08:00"))

    assert result["rows"][0]["net_yuan"] is None
    assert result["coverage"]["net_missing"] == 1
    assert result["zero_value_boundary_count"] == 0


def test_plate_quadrants_uses_latest_sample_per_plate_at_asof():
    from datetime import datetime
    rows = [{"code": f"{801000 + i}", "name": f"板块{i}", "strength": 1,
             "change_pct": 1.0, "amount_yuan": 2_000_000_000, "net_yuan": 1,
             "sampled_at": "2026-10-16T09:20:00"} for i in range(5000)]
    rows.extend([
        {"code": "801001", "name": "板块1", "strength": -1, "change_pct": -1,
         "amount_yuan": 2_000_000_000, "net_yuan": -1, "sampled_at": "2026-10-16T09:25:00"},
        {"code": "801002", "name": "板块2", "strength": 100, "change_pct": 10,
         "amount_yuan": 2_000_000_000, "net_yuan": 1, "sampled_at": "2026-10-16T09:30:00"},
    ])
    result = live_pages._plate_quadrants(
        {"top_plates_by_share": []}, {}, {}, rows, None,
        datetime.fromisoformat("2026-10-16T09:26:00+08:00"))
    assert result["coverage"]["rows"] == 5000
    assert result["counts"]["双正"] == 4999
    assert result["counts"]["双负"] == 1
