"""Small, opt-in email delivery for generated daily reports."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from pathlib import Path
from typing import Mapping, Sequence

from .email_render import RENDER_VERSION, _quality_banner, render_email_html

logger = logging.getLogger(__name__)
MAX_HTML_ATTACHMENT_BYTES = int(1.5 * 1024 * 1024)
ALLOWED_RECIPIENT = "412826313@qq.com"
DEFAULT_CONFIG_PATH = Path("/home/exedev/services/engine-next/shared/secrets/engine_next_notify.json")


def _load_matching_panorama(report_data: Mapping[str, object], report_dir: Path) -> dict | None:
    """Load the panorama saved beside this report and verify its content hash."""
    filename = report_data.get("auction_panorama_file")
    expected_hash = report_data.get("auction_panorama_sha256")
    trade_date = report_data.get("trade_date")
    asof = report_data.get("asof")
    if (filename != "auction_panorama.json" or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or not isinstance(trade_date, str) or not isinstance(asof, str)):
        return None
    try:
        raw = (report_dir / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            return None
        artifact = json.loads(raw)
        if (not isinstance(artifact, dict) or artifact.get("trade_date") != trade_date
                or artifact.get("asof") != asof or not isinstance(artifact.get("version"), str)
                or not isinstance(artifact.get("sha256"), str)):
            return None
        panorama_raw = json.dumps(artifact.get("panorama"), ensure_ascii=False,
                                  allow_nan=False, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(panorama_raw).hexdigest() != artifact["sha256"]:
            return None
        panorama = artifact.get("panorama")
        if (not isinstance(panorama, dict) or panorama.get("trade_date") != trade_date
                or panorama.get("asof") != asof):
            return None
        return panorama
    except (OSError, ValueError, TypeError):
        return None


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
    panorama_recovery_failed = False
    auction_stage = "auction" in str(stage).lower()
    if auction_stage:
        stored_panorama = _load_matching_panorama(report_data, json_path.parent) if json_path else None
        if stored_panorama is not None:
            report_data["auction_panorama"] = stored_panorama
        else:
            report_data.pop("auction_panorama", None)
            panorama_recovery_failed = True
            report_data["v6_panorama_failure"] = "V6_PANORAMA_MISSING:DATA_MISSING"
    elif not isinstance(report_data.get("auction_panorama"), Mapping):
        report_data.pop("auction_panorama", None)
    rendered_from_data, plain_text = render_email_html(report_data, stage, trade_date, manifest)
    # The report page is rendered from the full in-memory product before the
    # bounded JSON projection is persisted. Reuse that exact HTML for email.
    try:
        saved_html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"report HTML unavailable ({type(exc).__name__})") from None
    panorama = report_data.get("auction_panorama")
    quickbrief = panorama.get("quickbrief") if isinstance(panorama, Mapping) else None
    if not isinstance(quickbrief, Mapping):
        quickbrief = report_data.get("auction_quickbrief")
    brief_ready = ("auction" in str(stage).lower() and isinstance(quickbrief, Mapping)
                   and isinstance(quickbrief.get("email_lines"), list)
                   and bool(quickbrief.get("email_lines")))
    rendered_html = (rendered_from_data if brief_ready else
                     saved_html if "竞价全景 V6" in saved_html else rendered_from_data)
    if panorama_recovery_failed:
        rendered_html = saved_html
        notice = "报告目录中的 V6 全景文件缺失或校验失败，已降级：V6_PANORAMA_MISSING:DATA_MISSING"
        alert = ('<div role="alert" style="margin:10px;padding:12px;background:#fff0e5;'
                 'border:2px solid #c2410c;font-weight:700">' + notice + "</div>")
        rendered_html = (rendered_html.replace("</body>", alert + "</body>", 1)
                         if "</body>" in rendered_html else rendered_html + alert)
        plain_text = notice + "\n\n" + plain_text
    if "auction" in str(stage).lower() and not brief_ready:
        fallback_notice = "竞价速览构建失败,以下为完整报告回退。"
        if fallback_notice not in plain_text:
            plain_text = fallback_notice + "\n\n" + plain_text
        if fallback_notice not in rendered_html:
            rendered_html = rendered_html.replace(
                "</body>", '<div role="alert" style="padding:12px;background:#fff0e5;border:2px solid #c2410c;font-weight:700">'
                + fallback_notice + "</div></body>", 1)
    quality_status, _, _ = _quality_banner(manifest or {}, report_data)
    report_asof = (manifest or {}).get("asof") or report_data.get("asof")
    time_label = (str(report_asof)[11:16] if isinstance(report_asof, str) and len(report_asof) >= 16
                  else str(report_asof)[:5] if isinstance(report_asof, str) and len(report_asof) >= 5 else None)
    subject = f"{subject_prefix} [{quality_status}] [{trade_date}] {stage} {time_label or ''}报告".replace("  报告", " 报告")
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
    # Attachments are an explicit opt-in and are suppressed on degraded
    # auction fallbacks so the fallback remains self-contained.
    if attach_full_html and not panorama_recovery_failed and ("auction" not in str(stage).lower() or brief_ready):
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


def build_alert_message(trade_date: str, stage: str, error: BaseException,
                        to_addr: str, from_addr: str) -> EmailMessage:
    """Build a path-free alert with a stable, controlled failure fingerprint."""
    error_name = type(error).__name__.lower()
    error_text = str(error).lower()
    if "permission" in error_name or "permission" in error_text:
        kind = "PERMISSION"
    elif "timeout" in error_name or "timeout" in error_text:
        kind = "TIMEOUT"
    elif "missing" in error_name or "notfound" in error_name or "missing" in error_text:
        kind = "DATA_MISSING"
    else:
        kind = "INTERNAL_ERROR"
    missing = "检查阶段 manifest 与输入切片"
    message = EmailMessage()
    message["Subject"] = f"Core [ALERT] [{trade_date}] {stage} 阶段失败"
    message["Message-ID"] = make_msgid()
    message["From"] = from_addr
    message["To"] = to_addr
    message["X-Core-Alert-Fingerprint"] = hashlib.sha256(
        f"{kind}\0{missing}".encode("utf-8")).hexdigest()
    message.set_content(f"阶段：{stage}\n错误码：{kind}\n检查项：{missing}")
    message["X-Core-Alert"] = "stage-failure"
    return message


def _report_digest(message: EmailMessage) -> str:
    header_digest = message.get("X-Core-Report-SHA256")
    digest = hashlib.sha256()
    digest.update(str(header_digest or "").encode("ascii", errors="ignore"))
    digest.update(str(message.get("X-Core-Alert-Fingerprint", "")).encode("ascii", errors="ignore"))
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
    if not dry_run and not (os.environ.get("CORE_DATA_ROOT") and (Path(os.environ["CORE_DATA_ROOT"]).expanduser() / ".mail_enabled").is_file()):
        # fail closed: real mail needs CORE_DATA_ROOT set explicitly to a root holding this marker (production only); unset/temp/test roots never send
        logging.getLogger(__name__).warning("mail_enabled marker missing under CORE_DATA_ROOT; forcing dry-run")
        dry_run = True
    wire = msg.as_bytes()
    recipients = [part.strip() for part in str(msg.get("To", "")).split(",") if part.strip()]
    digest = _report_digest(msg)
    marker = _state_path(state_dir, msg, digest)
    if not dry_run:
        header_recipients = [address.strip().lower() for _, address in getaddresses(
            [str(msg.get(key, "")) for key in ("To", "Cc", "Bcc") if msg.get(key)])
                             if address.strip()]
        envelope = [address.strip().lower() for address in recipients]
        if header_recipients != [ALLOWED_RECIPIENT] or envelope != [ALLOWED_RECIPIENT]:
            raise ValueError("fixed recipient policy rejected message")
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
                client.send_message(msg, to_addrs=[ALLOWED_RECIPIENT])
        else:
            with smtplib.SMTP(config.host, config.port, timeout=10) as client:
                client.ehlo()
                if config.starttls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                client.login(config.user, config.password)
                client.send_message(msg, to_addrs=[ALLOWED_RECIPIENT])
    except Exception as exc:
        logger.error("report email delivery failed (%s)", type(exc).__name__)
        raise RuntimeError(f"SMTP delivery failed ({type(exc).__name__})") from None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"sha256": digest, "status": "sent"}, separators=(",", ":")), encoding="utf-8")
    return SendReceipt("sent", len(recipients), len(wire), round(time.monotonic() - started, 3),
                       msg.get("Message-ID"))

