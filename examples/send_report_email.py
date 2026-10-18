#!/usr/bin/env python3
"""Preview or explicitly send a generated report by email."""
import argparse
import json
from pathlib import Path

from engine_core.report_mailer import build_report_message, load_smtp_config, send_message

ALLOWED_RECIPIENT = "412826313@qq.com"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--to", required=True, action="append", metavar="ADDR")
    parser.add_argument("--allow", required=True, action="append", metavar="ADDR")
    parser.add_argument("--send", action="store_true", help="deliver email; default only previews")
    parser.add_argument("--attach-full-html", action="store_true", help="attach the full report.html when it is at most 1.5 MB")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if any(address.strip().lower() != ALLOWED_RECIPIENT for address in args.to):
        raise SystemExit("recipient policy allows only 412826313@qq.com")
    report_html = args.report_dir / "report.html"
    report_json = args.report_dir / "report.json"
    data = report_json if report_json.is_file() else None
    config = load_smtp_config() if args.send else None
    sender = config.from_addr if config else "reports@localhost"
    try:
        trade_date = json.loads((args.report_dir / "manifest.json").read_text(encoding="utf-8"))["trade_date"]
    except (OSError, ValueError, KeyError):
        raise SystemExit("report manifest is missing a valid trade_date")
    try:
        manifest = json.loads((args.report_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    msg = build_report_message("Core", trade_date, args.stage,
                               report_html, data, args.to, sender, manifest=manifest,
                               attach_full_html=args.attach_full_html)
    attachments = list(msg.iter_attachments())
    attached_bytes = sum(len(part.get_payload(decode=True) or b"") for part in attachments)
    plain = msg.get_body(preferencelist=("plain",)).get_content().splitlines()
    print(f"subject={msg['Subject']}")
    print(f"recipients={len(args.to)} attachment_bytes={attached_bytes}")
    print("body:")
    for line in plain:
        print(line)
    receipt = send_message(config, msg, dry_run=not args.send, allow_recipients=[ALLOWED_RECIPIENT])
    print(f"status={receipt.status} message_bytes={receipt.message_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
