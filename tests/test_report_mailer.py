import json
import pytest

from engine_core import report_mailer


def _config(port=465, starttls=False):
    return report_mailer.SmtpConfig(host="smtp.invalid", port=port, user="mailer",
                                    password="super-secret", from_addr="reports@example.test",
                                    starttls=starttls)


def _message(tmp_path, recipient=report_mailer.ALLOWED_RECIPIENT):
    html = tmp_path / "report.html"
    payload = tmp_path / "report.json"
    html.write_text("<h1>summary</h1>", encoding="utf-8")
    payload.write_text(json.dumps({"conclusion": "第一句。第二句。", "degraded": ["MISSING_X"],
                                   "count": 12}, ensure_ascii=False), encoding="utf-8")
    return report_mailer.build_report_message("Core", "2026-10-15", "auction", html, payload,
                                              [recipient], "reports@example.test")


def test_allowlist_rejects_before_transport(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("transport must not be constructed")
    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", forbidden)
    with pytest.raises(ValueError, match="fixed recipient policy"):
        report_mailer.send_message(_config(), _message(tmp_path, "other@example.test"), dry_run=False,
                                   allow_recipients=[report_mailer.ALLOWED_RECIPIENT], state_dir=tmp_path / "state")


def test_dry_run_does_not_touch_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mailer.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("SMTP touched"))
    receipt = report_mailer.send_message(_config(), _message(tmp_path), state_dir=tmp_path / "state")
    assert receipt.status == "dry_run"


def test_message_recovers_matching_panorama_for_compact_auction_body(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path / "empty-core-data"))
    quickbrief = {"email_lines": ["09:26 竞价速览 · 2026-10-16", "市场:覆盖不足,不下结论。",
        "主看:暂无", "备看:暂无", "风险:待核实", "阈值为 V1 冻结工程阈值,非市场规律,未回测"]}
    product = {"schema": "auction_panorama_v1", "trade_date": "2026-10-16",
        "asof": "2026-10-16T09:26:00+08:00", "overview": {"status": "PARTIAL"},
        "quickbrief": quickbrief, "plates": [], "yesterday_ladder": [], "lineage": {}}
    report = tmp_path / "reports"
    report.mkdir()
    (report / "report.html").write_text("<h1>full V6</h1>", encoding="utf-8")
    panorama_bytes = json.dumps(product, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sidecar = {"trade_date": product["trade_date"], "asof": product["asof"],
        "version": product["schema"], "sha256": hashlib.sha256(panorama_bytes).hexdigest(),
        "panorama": product}
    sidecar_bytes = json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(sidecar_bytes).hexdigest()
    (report / "auction_panorama.json").write_bytes(sidecar_bytes)
    (report / "report.json").write_text(json.dumps({"trade_date": "2026-10-16", "asof": product["asof"],
        "auction_quickbrief": quickbrief, "auction_panorama_file": "auction_panorama.json",
        "auction_panorama_sha256": digest}), encoding="utf-8")
    message = report_mailer.build_report_message("Core", "2026-10-16", "auction",
        report / "report.html", report / "report.json", [report_mailer.ALLOWED_RECIPIENT],
        "reports@example.test")
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "竞价速览卡" in html and "题材横向对比" in html
    assert "附件" not in message.get_body(preferencelist=("plain",)).get_content()
    assert "完整明细已存档于 Core 报告目录" in message.get_body(preferencelist=("plain",)).get_content()


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
                                         allow_recipients=["other@example.test"], state_dir=tmp_path / "state")
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
                                   allow_recipients=[report_mailer.ALLOWED_RECIPIENT], state_dir=tmp_path / "state")
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
    kwargs = dict(dry_run=False, allow_recipients=["other@example.test"], state_dir=tmp_path / "state")
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


def test_auction_report_has_no_attachment_or_attachment_copy_by_default(tmp_path):
    message = _message(tmp_path)
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == ["text/plain", "text/html"]
    assert list(message.iter_attachments()) == []
    assert "附件" not in message.get_body(preferencelist=("plain",)).get_content()
    assert "附件" not in message.get_body(preferencelist=("html",)).get_content()
    assert "V6_PANORAMA_MISSING:DATA_MISSING" in message.get_body(preferencelist=("html",)).get_content()


def test_attach_full_html_is_opt_in_and_size_limited(tmp_path):
    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    html_path.write_text("<h1>full</h1>", encoding="utf-8")
    json_path.write_text('{"conclusion":["ok"]}', encoding="utf-8")
    message = report_mailer.build_report_message("Core", "2026-10-15", "close", html_path, json_path,
                                                 ["me@example.test"], "reports@example.test",
                                                 attach_full_html=True)
    assert message.get_content_type() == "multipart/mixed"
    assert [part.get_filename() for part in message.iter_attachments()] == ["report.html"]

    html_path.write_bytes(b"x" * (report_mailer.MAX_HTML_ATTACHMENT_BYTES + 1))
    oversized = report_mailer.build_report_message("Core", "2026-10-15", "close", html_path, json_path,
                                                   ["me@example.test"], "reports@example.test",
                                                   attach_full_html=True)
    assert list(oversized.iter_attachments()) == []
    assert "超过 1.5 MB" in oversized.get_body(preferencelist=("plain",)).get_content()


def test_panorama_hash_mismatch_degrades_to_controlled_error(tmp_path):
    product = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:30:00+08:00",
               "quickbrief": {"email_lines": ["09:30 竞价速览"]}, "plates": []}
    report = tmp_path / "reports"
    report.mkdir()
    (report / "report.html").write_text("<h1>09:26 full V6</h1>", encoding="utf-8")
    import hashlib
    panorama_bytes = json.dumps(product, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
    sidecar = {"trade_date": product["trade_date"], "asof": product["asof"],
        "version": "auction_panorama_v1", "sha256": hashlib.sha256(panorama_bytes).hexdigest(),
        "panorama": product}
    sidecar_bytes = json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (report / "auction_panorama.json").write_bytes(sidecar_bytes)
    (report / "report.json").write_text(json.dumps({"trade_date": "2026-10-16",
        "asof": "2026-10-16T09:26:00+08:00", "auction_quickbrief": product["quickbrief"],
        "auction_panorama": product,
        "auction_panorama_file": "auction_panorama.json", "auction_panorama_sha256": "0" * 64}),
        encoding="utf-8")
    message = report_mailer.build_report_message("Core", "2026-10-16", "auction",
        report / "report.html", report / "report.json", [report_mailer.ALLOWED_RECIPIENT],
        "reports@example.test")
    html = message.get_body(preferencelist=("html",)).get_content()
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "V6_PANORAMA_MISSING:DATA_MISSING" in html
    assert "V6_PANORAMA_MISSING:DATA_MISSING" in plain
    assert "09:30 竞价速览" not in html
    attachments = list(message.iter_attachments())
    assert attachments == []


def test_fixed_recipient_cannot_be_changed_by_caller_allowlist(tmp_path, monkeypatch):
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
        allow_recipients=["other@example.test"], state_dir=state).status == "sent"
    second = _message(tmp_path, "other@example.test")
    with pytest.raises(ValueError, match="fixed recipient policy"):
        report_mailer.send_message(_config(), second, dry_run=False,
            allow_recipients=["other@example.test"], state_dir=state)
