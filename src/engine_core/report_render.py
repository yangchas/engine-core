"""Build-only HTML / EML renderings of a deterministic fact report.

No transport: nothing here opens a socket or sends mail (sending belongs to a
single, separately verified owner).  The renderings are deterministic functions
of the report: same report -> same bytes, with the report content hash carried
in the output so a rendering can be checked against its source facts.
"""

from __future__ import annotations

import html
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Tuple

from .clock import local_datetime_ms

RENDER_CONTRACT_VERSION = "ReportRenderV1"


def _body_lines(text: str) -> Tuple[Tuple[str, str], ...]:
    out = []
    for line in text.splitlines():
        if line.startswith("### "):
            out.append(("h3", line[4:]))
        elif line.startswith("## "):
            out.append(("h2", line[3:]))
        elif line.startswith("# "):
            out.append(("h1", line[2:]))
        elif line.startswith("- ") or line[:2].rstrip(".").isdigit() and ". " in line[:5]:
            out.append(("li", line[2:] if line.startswith("- ") else line))
        elif line.strip():
            out.append(("p", line))
    return tuple(out)


def render_report_html(report: Any) -> str:
    """HTML document for a ``MarketAuctionReport``-like object (text, hash, status)."""

    parts = []
    in_list = False
    for kind, value in _body_lines(report.text):
        if kind != "li" and in_list:
            parts.append("</ul>")
            in_list = False
        escaped = html.escape(value)
        if kind == "li":
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append("<li>%s</li>" % escaped)
        else:
            parts.append("<%s>%s</%s>" % (kind, escaped, kind))
    if in_list:
        parts.append("</ul>")
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        "<title>%s</title></head><body>\n%s\n"
        "<hr><p><small>status=%s content_hash=%s render=%s</small></p>\n</body></html>\n"
        % (
            html.escape("%s %s" % (report.trade_date, report.event_id)),
            "\n".join(parts), html.escape(str(report.status)),
            report.content_hash, RENDER_CONTRACT_VERSION,
        )
    )


def build_report_eml(
    report: Any, *, subject_prefix: str, sender: str, recipient: str
) -> bytes:
    """RFC 5322 message bytes with text and HTML alternatives (not sent)."""

    for name, value in (("subject_prefix", subject_prefix), ("sender", sender), ("recipient", recipient)):
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("%s must be a single-line non-empty string" % name)
    message = EmailMessage(policy=SMTP)
    message["Subject"] = "%s %s %s [%s]" % (
        subject_prefix.strip(), report.trade_date, report.event_id, report.status)
    message["From"] = sender.strip()
    message["To"] = recipient.strip()
    # deterministic Date: the report's own business day, not wall time
    from email.utils import format_datetime
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(
        local_datetime_ms(report.trade_date, "00:00:00") / 1000, tz=timezone.utc)
    message["Date"] = format_datetime(stamp)
    message["X-Core-Report-Hash"] = report.content_hash
    message["X-Core-Render"] = RENDER_CONTRACT_VERSION
    message.set_content(report.text)
    message.add_alternative(render_report_html(report), subtype="html")
    # a fixed boundary derived from the report hash keeps the bytes reproducible
    message.set_boundary("core-report-%s" % report.content_hash[:24])
    return message.as_bytes()


__all__ = ["RENDER_CONTRACT_VERSION", "build_report_eml", "render_report_html"]
