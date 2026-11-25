"""Recompute an auction universe comparison from sealed JSON captures only.

No Redis, TDengine, RabbitMQ, or production module is imported or contacted.
The TD source file is the captured 09:25 observation array with this stable
column order: ts, px_milli, chg_bp, match_amt_yuan, rest_bid_amt_yuan,
rest_ask_amt_yuan, symbol, trade_date, auction_tag. The anchor JSON is a
symbol-to-field-object mapping and each member must declare the requested tag;
the caller supplies the date from the separately reviewed capture manifest.
This offline auditor intentionally surfaces malformed-input errors. A future
production integration must catch those as diagnostic-unavailable records,
never let them suppress opening output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.auction_universe_reconciliation import reconcile_auction_universe


TD_COLUMNS = (
    "ts",
    "px_milli",
    "chg_bp",
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "symbol",
    "trade_date",
    "auction_tag",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, expected_sha256: str) -> Any:
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"SHA-256 mismatch for {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run(
    anchor_path: Path,
    td_path: Path,
    *,
    trade_date: str,
    auction_tag: str,
    anchor_sha256: str,
    td_sha256: str,
    sidecar_output: Path | None = None,
) -> dict[str, Any]:
    anchor_payload = _read_json(anchor_path, expected_sha256=anchor_sha256)
    td_payload = _read_json(td_path, expected_sha256=td_sha256)
    if not isinstance(anchor_payload, dict):
        raise ValueError("anchor capture must be a symbol-keyed JSON object")
    if not isinstance(td_payload, list):
        raise ValueError("TD capture must be a JSON array")

    normalized_anchor_date = str(trade_date).replace("-", "")
    anchor_symbols: list[str] = []
    for symbol, fields in anchor_payload.items():
        if not isinstance(fields, dict):
            raise ValueError(f"anchor member {symbol!r} must be a JSON object")
        if fields.get("tag") != auction_tag:
            raise ValueError(f"anchor member {symbol!r} has a different auction tag")
        anchor_symbols.append(symbol)

    td_rows: list[dict[str, Any]] = []
    for row_number, values in enumerate(td_payload):
        if not isinstance(values, list) or len(values) != len(TD_COLUMNS):
            raise ValueError(f"TD row {row_number} does not match the captured column schema")
        row = dict(zip(TD_COLUMNS, values))
        if str(row["trade_date"]).replace("-", "") != normalized_anchor_date:
            raise ValueError(f"TD row {row_number} has a different trade date")
        if row["auction_tag"] != auction_tag:
            raise ValueError(f"TD row {row_number} has a different auction tag")
        td_rows.append(row)

    anchor_snapshot = {
        "trade_date": trade_date,
        "auction_tag": auction_tag,
        "symbols": anchor_symbols,
    }
    first = reconcile_auction_universe(anchor_snapshot, td_rows)
    second = reconcile_auction_universe(anchor_snapshot, td_rows)
    report = {
        "contract": "AuctionUniverseCaptureAuditV1",
        "trade_date": first["trade_date"],
        "auction_tag": first["auction_tag"],
        "source_artifacts": {
            "anchor_path": str(anchor_path),
            "anchor_sha256": anchor_sha256,
            "td_path": str(td_path),
            "td_sha256": td_sha256,
        },
        "repeat_hash_equal": first["content_hash"] == second["content_hash"],
        "result": first,
    }
    if sidecar_output is not None:
        sidecar = {
            "contract": "OpeningStaleAuctionUniverseSidecarV1",
            "trade_date": first["trade_date"],
            "auction_tag": first["auction_tag"],
            "source_artifacts": report["source_artifacts"],
            "anchor_snapshot": anchor_snapshot,
            "td_rows": td_rows,
        }
        encoded = (
            json.dumps(
                sidecar,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with sidecar_output.open("x", encoding="utf-8") as output:
            output.write(encoded)
        report["sidecar_input"] = {
            "path": str(sidecar_output),
            "sha256": _sha256(sidecar_output),
            "anchor_symbol_count": len(anchor_symbols),
            "td_row_count": len(td_rows),
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-json", required=True, type=Path)
    parser.add_argument("--td-json", required=True, type=Path)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--auction-tag", required=True)
    parser.add_argument("--anchor-sha256", required=True)
    parser.add_argument("--td-sha256", required=True)
    parser.add_argument(
        "--sidecar-output",
        type=Path,
        help="optionally write a complete, hash-pinned Core runner sidecar using exclusive create",
    )
    args = parser.parse_args()
    report = run(
        args.anchor_json,
        args.td_json,
        trade_date=args.trade_date,
        auction_tag=args.auction_tag,
        anchor_sha256=args.anchor_sha256,
        td_sha256=args.td_sha256,
        sidecar_output=args.sidecar_output,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["repeat_hash_equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
