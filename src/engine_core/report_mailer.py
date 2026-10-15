"""Small, opt-in email delivery for generated daily reports."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Mapping, Sequence

from .email_render import RENDER_VERSION, render_email_html

logger = logging.getLogger(__name__)
MAX_HTML_ATTACHMENT_BYTES = int(1.5 * 1024 * 1024)
DEFAULT_CONFIG_PATH = Path("/home/exedev/services/engine-next/shared/secrets/engine_next_notify.json")


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    from_addr: str
    starttls: bool = False

    def __repr__(self) -> str:
        return (f"SmtpConfig(host={self.host!r}, port={self.port!r}, user={self.user!r}, "
                f"password=<redacted>, from_addr={self.from_addr!r}, starttls={self.starttls!r})")


@dataclass(frozen=True)
class SendReceipt:
    status: str
    recipient_count: int
    message_bytes: int
    elapsed_seconds: float
    message_id: str | None


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value.strip() if value is not None and value.strip() else None


def load_smtp_config(path: str | Path | None = None,
                     env: Mapping[str, str] | None = None) -> SmtpConfig:
    """Load the legacy-shaped config, applying ENGINE_NEXT_NOTIFY_* overrides."""
    environment = os.environ if env is None else env
    config_path = Path(path) if path is not None else Path(
        environment.get("ENGINE_NEXT_NOTIFY_CONFIG", str(DEFAULT_CONFIG_PATH)))
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except Exception as exc:
        raise ValueError(f"invalid SMTP configuration ({type(exc).__name__})") from None
    if not isinstance(raw, dict):
        raise ValueError("invalid SMTP configuration (expected object)")
    smtp = raw.get("smtp") if isinstance(raw.get("smtp"), dict) else {}
    values = {
        "host": _env_value(environment, "ENGINE_NEXT_NOTIFY_SMTP_HOST") or smtp.get("host"),
        "port": _env_value(environment, "ENGINE_NEXT_NOTIFY_SMTP_PORT") or smtp.get("port"),
        "user": _env_value(environment, "ENGINE_NEXT_NOTIFY_SMTP_USER") or smtp.get("user"),
        "password": _env_value(environment, "ENGINE_NEXT_NOTIFY_SMTP_PASSWORD") or smtp.get("password"),
        "from_addr": _env_value(environment, "ENGINE_NEXT_NOTIFY_EMAIL_FROM") or smtp.get("from"),
        "starttls": _env_value(environment, "ENGINE_NEXT_NOTIFY_SMTP_STARTTLS"),
    }
    if values["starttls"] is None:
        values["starttls"] = smtp.get("starttls", False)
    missing = [key for key in ("host", "port", "user", "password", "from_addr")
               if values[key] is None or (isinstance(values[key], str) and not values[key].strip())]
    if missing:
        raise ValueError("missing SMTP configuration fields: " + ", ".join(missing))
    try:
        port = int(values["port"])
        starttls = values["starttls"] if isinstance(values["starttls"], bool) else str(values["starttls"]).lower() in {"1", "true", "yes", "on"}
    except (TypeError, ValueError):
        raise ValueError("invalid SMTP configuration field: port") from None
    if not 1 <= port <= 65535:
        raise ValueError("invalid SMTP configuration field: port")
    return SmtpConfig(str(values["host"]).strip(), port, str(values["user"]).strip(),
                      str(values["password"]), str(values["from_addr"]).strip(), starttls)


def _walk_numbers(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_numbers(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        return
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield prefix, value


def _report_summary(report_path: Path | None) -> tuple[str, dict]:
    if report_path is None:
        return "报告 JSON 摘要不可用。", {}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "报告 JSON 摘要读取失败。", {}
    if not isinstance(data, dict):
        return "报告 JSON 摘要格式无效。", {}
    conclusion = data.get("conclusion")
    if not conclusion:
        for key in ("summary", "overall_conclusion", "headline"):
            if data.get(key):
                conclusion = data[key]
                break
    if isinstance(conclusion, (dict, list)):
        conclusion = json.dumps(conclusion, ensure_ascii=False)
    conclusion = str(conclusion or "未提供结论")
    sentences = [part.strip() for part in conclusion.replace("！", "。") .replace("？", "。").split("。") if part.strip()]
    lines = ["结论：" + "。".join(sentences[:3]) + ("。" if sentences else "")]
    numbers = list(_walk_numbers(data))[:8]
    if numbers:
        lines.append("关键数字：" + "；".join(f"{name}={value}" for name, value in numbers))
    gaps = data.get("degraded") or data.get("data_gaps") or data.get("missing") or []
    if isinstance(gaps, (str, int, float)):
        gaps = [gaps]
    if gaps:
        lines.append("数据缺口/降级：" + "；".join(map(str, gaps[:12])))
    return "\n".join(lines), data


def build_report_message(subject_prefix: str, trade_date: str, stage: str,
                         report_html_path: str | Path, report_json_path: str | Path | None,
                         to_addrs: Sequence[str], from_addr: str, *,
                         manifest: Mapping | None = None,
                         attach_full_html: bool = False) -> EmailMessage:
    html_path = Path(report_html_path)
    json_path = Path(report_json_path) if report_json_path is not None else None
    report_data = {}
    if json_path is not None:
        try:
            report_data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report_data = {}
    if not isinstance(report_data, dict):
        report_data = {}
    rendered_html, plain_text = render_email_html(report_data, stage, trade_date, manifest)
    subject = f"{subject_prefix} [{trade_date}] {stage} 报告"
    message = EmailMessage()
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message["From"] = from_addr
    message["To"] = ", ".join(str(address) for address in to_addrs)
    message.set_content(plain_text)
    message.add_alternative(rendered_html, subtype="html", charset="utf-8")
    report_digest = hashlib.sha256((RENDER_VERSION + "\0").encode("ascii"))
    if json_path is not None:
        try:
            report_digest.update(json_path.read_bytes())
        except OSError:
            pass
    message["X-Core-Report-SHA256"] = report_digest.hexdigest()
    if attach_full_html:
        try:
            html_bytes = html_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"report HTML unavailable ({type(exc).__name__})") from None
        if len(html_bytes) <= MAX_HTML_ATTACHMENT_BYTES:
            message.add_attachment(html_bytes, maintype="text", subtype="html", filename="report.html")
        else:
            message.get_body(preferencelist=("plain",)).set_content(
                plain_text + "\n\n完整 report.html 超过 1.5 MB，未附加。\n")
    return message


def _report_digest(message: EmailMessage) -> str:
    header_digest = message.get("X-Core-Report-SHA256")
    digest = hashlib.sha256()
    digest.update(str(header_digest or "").encode("ascii", errors="ignore"))
    digest.update(RENDER_VERSION.encode("ascii"))
    recipients = sorted({part.strip().lower() for part in str(message.get("To", "")).split(",") if part.strip()})
    digest.update(json.dumps(recipients, separators=(",", ":")).encode("utf-8"))
    for part in message.iter_attachments():
        digest.update(part.get_filename().encode("utf-8"))
        digest.update(part.get_payload(decode=True) or b"")
    return digest.hexdigest()


def _state_path(state_dir: str | Path | None, message: EmailMessage, digest: str) -> Path:
    root = Path(state_dir or os.environ.get("CORE_MAIL_STATE_DIR", "./.mail_state"))
    date_stage = (str(message["Subject"] or "").replace("/", "_").replace(" ", "_")[-80:] or "report")
    return root / f"{date_stage}-{digest}.json"


def send_message(config: SmtpConfig | None, msg: EmailMessage, *, dry_run: bool = True,
                 allow_recipients: Sequence[str] | None = None,
                 state_dir: str | Path | None = None) -> SendReceipt:
    started = time.monotonic()
    wire = msg.as_bytes()
    recipients = [part.strip() for part in str(msg.get("To", "")).split(",") if part.strip()]
    digest = _report_digest(msg)
    marker = _state_path(state_dir, msg, digest)
    if not dry_run:
        allow = {str(address).strip().lower() for address in (allow_recipients or ())}
        if not recipients or any(address.lower() not in allow for address in recipients):
            raise ValueError("recipient allowlist rejected one or more recipients")
    if marker.is_file():
        return SendReceipt("skipped", len(recipients), len(wire), 0.0, msg.get("Message-ID"))
    if dry_run:
        return SendReceipt("dry_run", len(recipients), len(wire), round(time.monotonic() - started, 3),
                           msg.get("Message-ID"))
    if config is None:
        raise ValueError("SMTP configuration is required for sending")
    try:
        if config.port == 465 and not config.starttls:
            with smtplib.SMTP_SSL(config.host, config.port, context=ssl.create_default_context(), timeout=10) as client:
                client.login(config.user, config.password)
                client.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=10) as client:
                client.ehlo()
                if config.starttls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                client.login(config.user, config.password)
                client.send_message(msg, to_addrs=recipients)
    except Exception as exc:
        logger.error("report email delivery failed (%s)", type(exc).__name__)
        raise RuntimeError(f"SMTP delivery failed ({type(exc).__name__})") from None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"sha256": digest, "status": "sent"}, separators=(",", ":")), encoding="utf-8")
    return SendReceipt("sent", len(recipients), len(wire), round(time.monotonic() - started, 3),
                       msg.get("Message-ID"))

