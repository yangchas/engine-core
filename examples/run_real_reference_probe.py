"""Bounded server-only connectivity probe for legacy reference-data sources.

This tool is an external composition check.  It imports the deployed
``engine_next`` connector package only after ``--legacy-root`` is supplied;
the ``engine_core`` package itself remains independent.  The probe calls
fetch/normalize methods and writes one local JSON evidence file.  It does not
assemble Redis/TD writers, recovery, notification or strategy components.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _row(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return {"value_type": type(value).__name__}


def _sample_fields(rows: list[Any], limit: int = 3) -> list[list[str]]:
    return [sorted(_row(item))[:30] for item in rows[:limit]]


def _sample_values(rows: list[Any], fields: tuple[str, ...], limit: int = 3) -> list[dict[str, Any]]:
    samples = []
    for item in rows[:limit]:
        value = _row(item)
        samples.append({field: value.get(field) for field in fields if field in value})
    return samples


def _run(source_id: str, operation) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = dict(operation())
        result.setdefault("connection_status", "PASS")
    except Exception as exc:  # pragma: no cover - exercised by server integration
        result = {
            "connection_status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    result["source_id"] = source_id
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def probe_sources(
    connectors: Mapping[str, Any],
    *,
    daily_request_factory,
    trade_date: str,
    symbol: str,
    max_rows: int,
) -> dict[str, Any]:
    """Run one bounded request per source and keep date claims explicit."""

    observations: dict[str, dict[str, Any]] = {}

    def baostock_probe() -> dict[str, Any]:
        rows = list(connectors["baostock"].fetch_daily_kline(
            daily_request_factory(symbol=symbol, trade_date=trade_date)
        ))
        returned_dates = sorted({str(_row(item).get("trade_date") or "") for item in rows})
        exact = bool(rows) and returned_dates == [trade_date]
        return {
            "getter": "fetch_daily_kline",
            "requested_trade_date": trade_date,
            "row_count": len(rows),
            "returned_trade_dates": returned_dates,
            "date_semantics": "EXPLICIT_REQUEST_AND_RESPONSE_DATE" if exact else "DATE_MISMATCH",
            "contract_status": "PASS" if exact else "FAIL",
            "sample_fields": _sample_fields(rows),
            "sample_values": _sample_values(rows, ("trade_date", "symbol", "close", "amount")),
        }

    observations["baostock_daily_kline"] = _run("baostock", baostock_probe)

    def kaipan_hot_probe() -> dict[str, Any]:
        rows = list(connectors["kaipan"].fetch_hot_plates(trade_date))[:max_rows]
        return {
            "getter": "fetch_hot_plates",
            "requested_trade_date": trade_date,
            "row_count": len(rows),
            "date_semantics": "EXPLICIT_REQUEST_RESPONSE_NOT_SELF_DATED",
            "contract_status": "OBSERVED" if rows else "MISSING",
            "sample_fields": _sample_fields(rows),
            "sample_values": _sample_values(rows, ("name", "rank", "strength", "change_pct")),
        }

    observations["kaipan_hot_plates"] = _run("kaipan", kaipan_hot_probe)

    first_pool_symbol: list[str] = []

    def kaipan_pool_probe() -> dict[str, Any]:
        rows = list(connectors["kaipan"].fetch_yesterday_bans_pool(trade_date, max_ban=3))
        rows = rows[:max_rows]
        if rows:
            code = str(_row(rows[0]).get("code") or _row(rows[0]).get("symbol") or "")[-6:]
            if code:
                first_pool_symbol.append(code)
        return {
            "getter": "fetch_yesterday_bans_pool",
            "requested_trade_date": trade_date,
            "row_count": len(rows),
            "date_semantics": "EXPLICIT_REQUEST_RESPONSE_DATE_NOT_REQUIRED",
            "contract_status": "OBSERVED" if rows else "MISSING",
            "sample_fields": _sample_fields(rows),
            "sample_values": _sample_values(rows, ("code", "name", "lb_days", "plate", "date")),
        }

    observations["kaipan_yesterday_limit_pool"] = _run("kaipan", kaipan_pool_probe)

    def kaipan_reason_probe() -> dict[str, Any]:
        target = first_pool_symbol[0] if first_pool_symbol else symbol
        rows = list(connectors["kaipan"].fetch_ban_reasons(target))[:max_rows]
        source_dates = sorted({
            str(_row(item).get("source_trade_date") or "") for item in rows
            if _row(item).get("source_trade_date")
        })
        exact = bool(rows) and source_dates == [trade_date]
        return {
            "getter": "fetch_ban_reasons",
            "requested_symbol": target,
            "requested_trade_date": None,
            "row_count": len(rows),
            "returned_source_trade_dates": source_dates,
            "date_semantics": (
                "RESPONSE_DATE_MATCHES_AUDIT_TARGET" if exact
                else "CURRENT_QUERY_DATE_UNKNOWN_OR_DIFFERENT"
            ),
            "contract_status": "PASS" if exact else ("OBSERVED" if rows else "MISSING"),
            "sample_fields": _sample_fields(rows),
            "sample_values": _sample_values(rows, ("symbol", "reason", "source_trade_date")),
        }

    observations["kaipan_ban_reasons"] = _run("kaipan", kaipan_reason_probe)

    def wencai_probe() -> dict[str, Any]:
        frame = asyncio.run(connectors["wencai"].fetch_limitup_with_lb_days(
            max_stocks=max_rows
        ))
        normalized = list(connectors["wencai"].normalize_limitup_with_lb_days(frame))
        return {
            "getter": "fetch_limitup_with_lb_days",
            "requested_trade_date": None,
            "audit_context_trade_date": trade_date,
            "row_count": len(normalized),
            "date_semantics": "CURRENT_QUERY_NO_STRUCTURED_DATE",
            "historical_runtime_role": "UNAVAILABLE_WITHOUT_DATED_QUERY_EVIDENCE",
            "contract_status": "OBSERVED" if normalized else "MISSING",
            "sample_fields": _sample_fields(normalized),
            "sample_values": _sample_values(normalized, ("symbol", "lb_days", "source")),
        }

    observations["wencai_limit_truth"] = _run("wencai", wencai_probe)

    def ths_probe() -> dict[str, Any]:
        rows = list(asyncio.run(connectors["ths"].fetch_hot_rank(top_n=max_rows)))
        normalized = list(connectors["ths"].normalize_hot_rank(rows))
        return {
            "getter": "fetch_hot_rank",
            "requested_trade_date": None,
            "audit_context_trade_date": trade_date,
            "row_count": len(normalized),
            "date_semantics": "CURRENT_QUERY_NO_DATE",
            "historical_runtime_role": "UNAVAILABLE",
            "contract_status": "OBSERVED" if normalized else "MISSING",
            "sample_fields": _sample_fields(normalized),
            "sample_values": _sample_values(normalized, ("symbol", "rank", "heat", "name")),
        }

    observations["ths_hot_rank"] = _run("ths", ths_probe)

    return {
        "contract_version": "RealReferenceSourceProbeV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "observations": observations,
        "connection_pass_count": sum(
            item["connection_status"] == "PASS" for item in observations.values()
        ),
        "connection_total_count": len(observations),
        "side_effect_boundary": (
            "connector fetch and pure normalization only; no Redis/TD writer, "
            "repair, notification, SMTP or strategy component assembled"
        ),
    }


def _load_connectors(legacy_root: Path):
    resolved = legacy_root.resolve()
    if not (resolved / "engine_next" / "connectors").is_dir():
        raise ValueError("legacy-root must contain engine_next/connectors")
    sys.path.insert(0, str(resolved))
    from engine_next.connectors import (  # pylint: disable=import-outside-toplevel
        BaostockConnector,
        KaipanConnector,
        ThsHotConnector,
        WencaiConnector,
    )
    from engine_next.contracts.baostock_contracts import (  # pylint: disable=import-outside-toplevel
        BaostockDailyKlineRequest,
    )
    return {
        "baostock": BaostockConnector(),
        "kaipan": KaipanConnector(),
        "wencai": WencaiConnector(),
        "ths": ThsHotConnector(),
    }, BaostockDailyKlineRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--legacy-release-commit", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", default="600000")
    parser.add_argument("--max-rows", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    datetime.strptime(args.trade_date, "%Y-%m-%d")
    if len(args.symbol) != 6 or not args.symbol.isdigit():
        parser.error("symbol must be a six-digit code")
    if not 1 <= args.max_rows <= 50:
        parser.error("max-rows must be between 1 and 50")
    connectors, request_factory = _load_connectors(args.legacy_root)
    result = probe_sources(
        connectors,
        daily_request_factory=request_factory,
        trade_date=args.trade_date,
        symbol=args.symbol,
        max_rows=args.max_rows,
    )
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["legacy_root"] = str(args.legacy_root.resolve())
    result["legacy_release_commit"] = args.legacy_release_commit
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["connection_pass_count"] == result["connection_total_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
