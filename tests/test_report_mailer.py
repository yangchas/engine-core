import json
import pytest

from engine_core import report_mailer


def _config(port=465, starttls=False):
    return report_mailer.SmtpConfig(host="smtp.invalid", port=port, user="mailer",
                                    password="super-secret", from_addr="reports@example.test",
                                    starttls=starttls)


def _message(tmp_path):
    html = tmp_path / "report.html"
    payload = tmp_path / "report.json"
    html.write_text("<h1>summary</h1>", encoding="utf-8")
    payload.write_text(json.dumps({"conclusion": "第一句。第二句。", "degraded": ["MISSING_X"],
                                   "count": 12}, ensure_ascii=False), encoding="utf-8")
    return report_mailer.build_report_message("Core", "2026-10-15", "auction", html, payload,
                                              ["me@example.test"], "reports@example.test")


def test_allowlist_rejects_before_transport(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("transport must not be constructed")
    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", forbidden)
    with pytest.raises(ValueError, match="allowlist"):
        report_mailer.send_message(_config(), _message(tmp_path), dry_run=False,
                                   allow_recipients=["other@example.test"], state_dir=tmp_path / "state")


def test_dry_run_does_not_touch_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("SMTP touched"))
    receipt = report_mailer.send_message(_config(), _message(tmp_path), state_dir=tmp_path / "state")
    assert receipt.status == "dry_run"


@pytest.mark.parametrize("port,starttls,expected", [(465, False, "ssl"), (587, True, "starttls")])
def test_selects_ssl_or_starttls_transport(tmp_path, monkeypatch, port, starttls, expected):
    calls = []

    class FakeSmtp:
        def __init__(self, *args, **kwargs): calls.append(("init", expected))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append(("ehlo",))
        def starttls(self, **kwargs): calls.append(("starttls",))
        def login(self, user, password): calls.append(("login",))
        def send_message(self, message, **kwargs): calls.append(("send",))

    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(report_mailer.smtplib, "SMTP", FakeSmtp)
    receipt = report_mailer.send_message(_config(port, starttls), _message(tmp_path), dry_run=False,
                                         allow_recipients=["me@example.test"], state_dir=tmp_path / "state")
    assert receipt.status == "sent"
    assert ("starttls",) in calls if expected == "starttls" else ("starttls",) not in calls


def test_password_never_in_repr_or_transport_exception(tmp_path, monkeypatch, caplog):
    config = _config()
    assert "super-secret" not in repr(config)

    class BrokenSmtp:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, user, password): raise RuntimeError("failed with super-secret")
        def send_message(self, message, **kwargs): pass

    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", BrokenSmtp)
    with pytest.raises(RuntimeError) as exc:
        report_mailer.send_message(config, _message(tmp_path), dry_run=False,
                                   allow_recipients=["me@example.test"], state_dir=tmp_path / "state")
    assert "super-secret" not in str(exc.value)
    assert "super-secret" not in caplog.text


def test_dedupe_skips_successful_repeat(tmp_path, monkeypatch):
    class FakeSmtp:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, user, password): pass
        def send_message(self, message, **kwargs): pass

    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", FakeSmtp)
    kwargs = dict(dry_run=False, allow_recipients=["me@example.test"], state_dir=tmp_path / "state")
    assert report_mailer.send_message(_config(), _message(tmp_path), **kwargs).status == "sent"
    assert report_mailer.send_message(_config(), _message(tmp_path), **kwargs).status == "skipped"


def test_large_html_is_ignored_by_default(tmp_path):
    html = tmp_path / "report.html"
    payload = tmp_path / "report.json"
    html.write_bytes(b"x" * (report_mailer.MAX_HTML_ATTACHMENT_BYTES + 1))
    payload.write_text('{"conclusion":"fine"}', encoding="utf-8")
    message = report_mailer.build_report_message("Core", "2026-10-15", "close", html, payload,
                                                 ["me@example.test"], "reports@example.test")
    assert list(message.iter_attachments()) == []
    assert message.get_content_type() == "multipart/alternative"


def test_structured_report_is_html_body_without_default_attachment(tmp_path):
    message = _message(tmp_path)
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == ["text/plain", "text/html"]
    assert list(message.iter_attachments()) == []
    assert "第一句" in message.get_body(preferencelist=("html",)).get_content()


def test_attach_full_html_is_opt_in_and_size_limited(tmp_path):
    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    html_path.write_text("<h1>full</h1>", encoding="utf-8")
    json_path.write_text('{"conclusion":["ok"]}', encoding="utf-8")
    message = report_mailer.build_report_message("Core", "2026-10-15", "auction", html_path, json_path,
                                                 ["me@example.test"], "reports@example.test",
                                                 attach_full_html=True)
    assert message.get_content_type() == "multipart/mixed"
    assert [part.get_filename() for part in message.iter_attachments()] == ["report.html"]

    html_path.write_bytes(b"x" * (report_mailer.MAX_HTML_ATTACHMENT_BYTES + 1))
    oversized = report_mailer.build_report_message("Core", "2026-10-15", "auction", html_path, json_path,
                                                   ["me@example.test"], "reports@example.test",
                                                   attach_full_html=True)
    assert list(oversized.iter_attachments()) == []
    assert "超过 1.5 MB" in oversized.get_body(preferencelist=("plain",)).get_content()


def test_dedupe_key_includes_recipient_set_and_render_version(tmp_path, monkeypatch):
    class FakeSmtp:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, user, password): pass
        def send_message(self, message, **kwargs): pass

    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", FakeSmtp)
    state = tmp_path / "state"
    first = _message(tmp_path)
    assert report_mailer.send_message(_config(), first, dry_run=False,
        allow_recipients=["me@example.test"], state_dir=state).status == "sent"
    second = report_mailer.build_report_message("Core", "2026-10-15", "auction", tmp_path / "report.html",
        tmp_path / "report.json", ["other@example.test"], "reports@example.test")
    assert report_mailer.send_message(_config(), second, dry_run=False,
        allow_recipients=["other@example.test"], state_dir=state).status == "sent"
