import email
from email import policy

import pytest

from engine_core import build_report_eml, render_report_html
from engine_core.market_auction_report import build_market_auction_report


def _report():
    return build_market_auction_report(
        trade_date="2026-10-13", event_id="AUCTION_0925", data_origin="T<&>",
        notes=("a <b> note",),
    )


def test_html_escapes_and_carries_hash_and_status():
    report = _report()
    out = render_report_html(report)
    assert "&lt;&amp;&gt;" in out and "<b>" not in out
    assert report.content_hash in out and "status=%s" % report.status in out
    assert render_report_html(report) == out  # deterministic


def test_eml_is_deterministic_multipart_and_not_sent():
    report = _report()
    raw = build_report_eml(report, subject_prefix="[Core]", sender="core@example.invalid",
                           recipient="audit@example.invalid")
    assert raw == build_report_eml(report, subject_prefix="[Core]", sender="core@example.invalid",
                                   recipient="audit@example.invalid")
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["X-Core-Report-Hash"].strip() == report.content_hash
    assert "2026-10-13 AUCTION_0925" in msg["Subject"]
    assert msg["Date"].startswith("Mon, 12 Oct 2026 16:00:00") or "2026" in msg["Date"]
    kinds = {part.get_content_type() for part in msg.walk()}
    assert {"text/plain", "text/html"} <= kinds
    assert "竞价事实报告" in msg.get_body(preferencelist=("plain",)).get_content()


def test_header_injection_is_rejected():
    with pytest.raises(ValueError):
        build_report_eml(_report(), subject_prefix="x\nBcc: a@b", sender="s@x", recipient="r@x")
