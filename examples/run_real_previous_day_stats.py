"""Run ``PreviousDayStatsFunction`` against the deployed TD read path.

The query shape follows the verified ``engine_next`` ``TDengineService``
``get_daily_kline`` path (per-symbol ``d_<code>`` table, bounded date range),
but the result is passed through the independent core Provider/DataFunction
contracts.  This command is read-only and intentionally reports
``UNAVAILABLE`` when historical ``available_at`` evidence is absent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (
    DataContext,
    DataRequest,
    PreviousDayStatsFunction,
    TDPreviousDayStatsProvider,
    build_calendar_snapshot,
    canonical_json,
)


def _symbol(value: str) -> str:
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("symbol must be a six-digit stock code")
    return value


def _date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def _fetch_td_rows(
    previous_trade_date: str,
    symbols: tuple[str, ...],
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    config: str,
    timezone_name: str,
) -> list[dict[str, Any]]:
    """Use the legacy TD query shape without importing its mutating service."""

    previous_trade_date = _date(previous_trade_date)
    if not symbols:
        raise ValueError("a bounded symbol set is required for the live probe")
    import taos  # type: ignore[import-not-found]

    connection = taos.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        config=config,
        timezone=timezone_name,
    )
    try:
        cursor = connection.cursor()
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            symbol = _symbol(symbol)
            table = "d_" + symbol
            # The legacy service uses inclusive date bounds and per-symbol
            # child tables.  Identifiers are validated before interpolation.
            sql = (
                "SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg "
                f"FROM {table} WHERE ts >= '{previous_trade_date}' "
                f"AND ts <= '{previous_trade_date}' ORDER BY ts"
            )
            cursor.execute(sql)
            for row in cursor.fetchall():
                rows.append(
                    {
                        "symbol": symbol,
                        "close": row[4],
                        "amount": row[6],
                        "volume": row[5],
                    }
                )
        return rows
    finally:
        connection.close()


def run_real_previous_day_stats(
    *,
    trade_date: str,
    previous_trade_date: str,
    symbols: tuple[str, ...],
    observed_at: datetime,
    td_kwargs: dict[str, Any],
    fetch_rows_override: Any = None,
) -> dict[str, Any]:
    """Execute the core data function with real TD rows and a fixed calendar."""

    observed_at_ms = int(observed_at.timestamp() * 1000)
    calendar = build_calendar_snapshot(
        [previous_trade_date, trade_date],
        version="real-probe-" + trade_date,
        declared_valid_from=trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=previous_trade_date,
        source_guard_valid_to=trade_date,
        source_id="explicit_probe_calendar",
        observed_at_ms=observed_at_ms,
        evidence_ref="probe://explicit-trade-date-pair",
    )
    rows_seen: list[dict[str, Any]] = []

    def fetch_rows(previous: str, requested: tuple[str, ...]):
        rows = (
            fetch_rows_override(previous, requested)
            if fetch_rows_override is not None
            else _fetch_td_rows(previous, requested, **td_kwargs)
        )
        rows_seen.extend(rows)
        return rows

    provider = TDPreviousDayStatsProvider(
        fetch_rows,
        observed_at_ms=lambda: observed_at_ms,
        # No historical publication evidence is invented by this probe.
        available_at_ms=None,
        source_id="tdengine_daily_kline",
        source_schema="daily_kline",
        evidence_ref="td://market_data1/daily_kline/" + previous_trade_date,
    )
    function = PreviousDayStatsFunction(provider, calendar)
    request = DataRequest(
        request_id="real-previous-day-" + trade_date,
        function_id="previous_day_stats",
        trade_date=trade_date,
        effective_as_of_ms=observed_at_ms,
        knowledge_as_of_ms=observed_at_ms,
        symbols=tuple(sorted(symbols)),
    )
    result = function.execute(
        DataContext("real-previous-day-probe", "READ_ONLY", observed_at_ms),
        request,
    )
    return {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "symbols": tuple(sorted(symbols)),
        "rows_seen": len(rows_seen),
        "result_status": result.status,
        "actual_source": result.actual_source,
        "actual_trade_date": result.actual_trade_date,
        "requested_trade_date": result.requested_trade_date,
        "completeness": result.completeness,
        "missing_fields": result.missing_fields,
        "missing_symbols": result.missing_symbols,
        "data": json.loads(canonical_json(result.data)) if result.data is not None else None,
        "available_at_ms": result.available_at_ms,
        "observed_at_ms": result.observed_at_ms,
        "content_hash": result.content_hash,
        "calendar_semantic_hash": calendar.semantic_hash,
        "read_only": True,
        "side_effect_boundary": "TD SELECT only; no Redis/Rabbit/TD write/repair/notification",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _date(args.trade_date)
    previous_trade_date = _date(args.previous_trade_date)
    symbols = tuple(sorted({_symbol(item.strip()) for item in args.symbols.split(",") if item.strip()}))
    observed_at = datetime.now(timezone.utc)
    result = run_real_previous_day_stats(
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        symbols=symbols,
        observed_at=observed_at,
        td_kwargs={
            "host": os.environ.get("TDENGINE_HOST", "127.0.0.1"),
            "port": int(os.environ.get("TDENGINE_PORT", "6030")),
            "user": os.environ.get("TDENGINE_USER", "root"),
            "password": os.environ.get("TDENGINE_PASSWORD", "taosdata"),
            "database": os.environ.get("TDENGINE_DATABASE", "market_data1"),
            "config": os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
            "timezone_name": os.environ.get("TDENGINE_TIMEZONE", "Asia/Shanghai"),
        },
    )
    result["observed_at"] = observed_at.isoformat()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
