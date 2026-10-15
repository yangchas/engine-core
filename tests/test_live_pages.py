import json

from engine_core import live_pages
from examples import render_live_pages as compatibility_cli


def test_migrated_renderer_compatibility_json_matches_fixture_fields():
    td = type("FixtureTD", (), {"states_at": lambda self, cutoff: {}})()
    _, from_library = live_pages.render_page(td, trade_date="2026-10-15", stage="auction_0926")
    _, from_cli = compatibility_cli.render_page(td, trade_date="2026-10-15", stage="auction_0926")
    # Timings are observational and intentionally vary between invocations.
    for payload in (from_library, from_cli):
        payload["manifest"].pop("timings_ms", None)
    library_json = live_pages._json_report_payload(from_library)
    cli_json = compatibility_cli._json_report_payload(from_cli)
    assert json.loads(json.dumps(library_json, ensure_ascii=False, allow_nan=False)) == \
        json.loads(json.dumps(cli_json, ensure_ascii=False, allow_nan=False))
    assert compatibility_cli.render_page is live_pages.render_page
