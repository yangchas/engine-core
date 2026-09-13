"""Build a versioned, read-only BaoStock trading-calendar evidence artifact.

The probe is an external preparation command.  Runtime consumers use the
resulting immutable snapshot and do not contact BaoStock.  ``rows_returned``
counts all source calendar rows; ``trading_dates_count`` counts only rows whose
``is_trading_day`` flag is ``1``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import build_calendar_snapshot, canonical_json


def _date_text(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def build_calendar_probe_result(
    rows: Iterable[Mapping[str, Any]],
    *,
    query_start: str,
    query_end: str,
    version: str,
    declared_valid_from: str,
    declared_valid_to: str,
    observed_at_ms: int,
) -> dict[str, Any]:
    """Validate source rows and build a canonical snapshot description."""

    query_start = _date_text(query_start)
    query_end = _date_text(query_end)
    declared_valid_from = _date_text(declared_valid_from)
    declared_valid_to = _date_text(declared_valid_to)
    if query_start > query_end:
        raise ValueError("query date bounds are inverted")
    source_rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        calendar_date = _date_text(str(raw.get("calendar_date") or "").strip())
        if not query_start <= calendar_date <= query_end:
            raise ValueError("source row is outside query bounds")
        if calendar_date in seen:
            raise ValueError("duplicate calendar_date in source response")
        seen.add(calendar_date)
        flag = str(raw.get("is_trading_day") or "").strip()
        if flag not in {"0", "1"}:
            raise ValueError("is_trading_day must be 0 or 1")
        source_rows.append({"calendar_date": calendar_date, "is_trading_day": flag})

    source_rows.sort(key=lambda item: item["calendar_date"])
    expected_dates = {
        (date.fromisoformat(query_start) + timedelta(days=offset)).isoformat()
        for offset in range((date.fromisoformat(query_end) - date.fromisoformat(query_start)).days + 1)
    }
    actual_dates = {item["calendar_date"] for item in source_rows}
    if not source_rows:
        raise ValueError("empty calendar source response")
    if actual_dates != expected_dates:
        raise ValueError("calendar source response has date gaps")
    trading_dates = [
        item["calendar_date"] for item in source_rows if item["is_trading_day"] == "1"
    ]
    raw_rows_hash = hashlib.sha256(canonical_json(source_rows).encode("utf-8")).hexdigest()
    evidence_ref = f"baostock://query_trade_dates/{query_start}/{query_end}"
    snapshot = build_calendar_snapshot(
        trading_dates,
        version=version,
        declared_valid_from=declared_valid_from,
        declared_valid_to=declared_valid_to,
        source_guard_valid_from=query_start,
        source_guard_valid_to=query_end,
        source_id="baostock",
        observed_at_ms=observed_at_ms,
        evidence_ref=evidence_ref,
    )
    return {
        "contract_version": "RealCalendarProbeV1",
        "source_id": "baostock",
        "query_start": query_start,
        "query_end": query_end,
        "version": version,
        "declared_valid_from": declared_valid_from,
        "declared_valid_to": declared_valid_to,
        "observed_at_ms": observed_at_ms,
        "rows_returned": len(source_rows),
        "trading_dates_count": len(trading_dates),
        "raw_rows_sha256": raw_rows_hash,
        "trading_dates": trading_dates,
        "calendar_semantic_hash": snapshot.semantic_hash,
        "calendar_evidence_hash": snapshot.evidence_hash,
        "evidence_ref": evidence_ref,
        "raw_rows": source_rows,
        "side_effect_boundary": "BaoStock login/query/logout only; no Redis/TD/file writer",
    }


def _query_baostock(start_date: str, end_date: str) -> list[dict[str, str]]:
    import baostock as bs  # type: ignore[import-not-found]

    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    try:
        result = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        if str(result.error_code) != "0":
            raise RuntimeError(f"BaoStock calendar query failed: {result.error_code} {result.error_msg}")
        frame = result.get_data()
        return [
            {
                "calendar_date": str(row["calendar_date"]),
                "is_trading_day": str(row["is_trading_day"]),
            }
            for _, row in frame.iterrows()
        ]
    finally:
        bs.logout()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--declared-from")
    parser.add_argument("--declared-to")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start_date = _date_text(args.start_date)
    end_date = _date_text(args.end_date)
    observed_at = datetime.now(timezone.utc)
    result = build_calendar_probe_result(
        _query_baostock(start_date, end_date),
        query_start=start_date,
        query_end=end_date,
        version=args.version,
        declared_valid_from=args.declared_from or start_date,
        declared_valid_to=args.declared_to or end_date,
        observed_at_ms=int(observed_at.timestamp() * 1000),
    )
    result["observed_at"] = observed_at.isoformat()
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
