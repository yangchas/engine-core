import json

import pytest

from examples.render_live_pages import render_page, write_page


def test_render_page_has_sections_and_self_contained_assets():
    td = type("TD", (), {"states_at": lambda self, cutoff: {}})()
    page, payload = render_page(td, trade_date="2026-10-15", stage="auction_0926", previous_day_final_yuan=None)
    for section in ("大盘", "板块份额", "封涨停质量", "昨日高标", "竞价内变化", "看A做B", "近72日"):
        assert section in page
    assert "<style>" in page and "@media" in page
    assert payload["limitations"]


def test_write_page_refuses_nonempty_directory(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(FileExistsError):
        write_page(output, "page", {})
