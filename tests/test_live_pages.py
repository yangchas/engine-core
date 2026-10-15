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
