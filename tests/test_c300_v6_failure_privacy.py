import json

from engine_core.email_render import render_email_html, v6_failure_internal, v6_failure_public_code
from engine_core.report_mailer import build_alert_message, build_report_message


def test_fallback_attachment_does_not_leak_exception_path(tmp_path):
    raw = ("PermissionError: denied /home/alice/private/data.csv "
           r"C:\Users\alice\secret\input.csv /tmp/c299-audit/private.json")
    html, plain = render_email_html({"v6_panorama_failure": raw}, "auction", "2026-10-19")

    assert "V6_PANORAMA_MISSING:PERMISSION" in html
    assert "V6_PANORAMA_MISSING:PERMISSION" in plain
    for secret in ("/home/alice", r"C:\Users\alice", "/tmp/c299-audit"):
        assert secret not in html
        assert secret not in plain

    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(json.dumps({"v6_panorama_failure": "V6_PANORAMA_MISSING:PERMISSION"}),
                         encoding="utf-8")
    message = build_report_message("Core", "2026-10-19", "auction", html_path, json_path,
                                   ["reports@example.test"], "reports@example.test")
    body = message.get_body(preferencelist=("html",)).get_content()
    attachment = next(message.iter_attachments()).get_content()
    for rendered in (body, attachment):
        assert "V6_PANORAMA_MISSING:PERMISSION" in rendered
        for secret in ("/home/alice", r"C:\\Users\\alice", "/tmp/c299-audit"):
            assert secret not in rendered

    outward_json = json.dumps({"v6_panorama_failure": v6_failure_public_code(raw)})
    internal_detail = v6_failure_internal(raw)
    assert "V6_PANORAMA_MISSING:PERMISSION" in outward_json
    assert all(secret not in outward_json and secret not in internal_detail
               for secret in ("/home/alice", r"C:\Users\alice", "/tmp/c299-audit"))

    alert = build_alert_message("2026-10-19", "auction", PermissionError(raw),
                                "reports@example.test", "reports@example.test")
    assert "PERMISSION" in alert.get_content()
    assert all(secret not in alert.get_content()
               for secret in ("/home/alice", r"C:\Users\alice", "/tmp/c299-audit"))
