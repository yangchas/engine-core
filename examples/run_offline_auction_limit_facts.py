"""Read-only TD extraction and offline auction limit/seal facts."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core.auction_limit_facts import (
    aggregate_plate_auction_limit_facts, derive_stock_auction_limit_fact,
    plate_fact_to_dict, stock_fact_to_dict,
)
from engine_core.plate_membership import (
    PlateHierarchy, apply_hierarchy, build_memberships, default_alias_hierarchy,
)
from examples.run_offline_plate_pipeline import (
    _connect_td, _is_missing_table_error, _load_snapshot, _snapshot_asof, _snapshot_sealed_at,
    ensure_empty_output_dir,
)


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                allow_nan=False) + "\n", encoding="utf-8")


def _point_in_time_flags(trade_date: str, snapshot_asof: str | None,
                         snapshot_sealed_at: datetime | None) -> list[str]:
    tags = {"0920": "09:20:00", "0924": "09:24:00", "0925": "09:25:00"}
    if snapshot_asof is None or snapshot_sealed_at is None:
        return list(tags)
    flagged = []
    for tag, cutoff_time in tags.items():
        cutoff = datetime.fromisoformat(f"{trade_date}T{cutoff_time}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if snapshot_sealed_at > cutoff:
            flagged.append(tag)
    return flagged


def _snapshot_memberships(snapshot: Mapping[str, Any]):
    stock_plate = snapshot.get("stock_plate", {})
    s2p = snapshot.get("s2p", {})
    symbols = sorted(set(map(str, stock_plate)) | set(map(str, s2p)))
    themes = {
        symbol: list(dict.fromkeys(
            ([stock_plate.get(symbol)] if stock_plate.get(symbol) else []) + list(s2p.get(symbol, ()))
        ))
        for symbol in symbols
    }
    built = build_memberships(symbols=symbols, pool_plate_by_symbol={},
                              reason_candidates_by_symbol={}, static_themes_by_symbol=themes)
    hierarchy = default_alias_hierarchy()
    return symbols, apply_hierarchy(built, hierarchy), hierarchy


def _read_day(host: str, trade_date: str, kline_start: str, kline_end: str,
              symbols: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    connection, database = _connect_td(host)
    try:
        cursor = connection.cursor()
        compact = trade_date.replace("-", "")
        snapshots: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
        auction_row_count = 0
        for tag in ("0920", "0924", "0925"):
            cursor.execute(
                "SELECT symbol, px_milli, match_amt_yuan, rest_bid_amt_yuan, "
                "rest_ask_amt_yuan, limit_state "
                f'FROM {database}.auction_snapshot_v2 WHERE trade_date="{compact}" AND auction_tag="{tag}"'
            )
            for symbol, px, match, bid, ask, state in cursor.fetchall():
                key = str(symbol)
                if key in snapshots:
                    snapshots[key][tag] = {
                        "px_milli": px, "match_amt_yuan": match,
                        "rest_bid_amt_yuan": bid, "rest_ask_amt_yuan": ask,
                        "limit_state": state,
                    }
                    auction_row_count += 1

        cutoff = min(date.fromisoformat(kline_end), date.fromisoformat(trade_date).fromordinal(
            date.fromisoformat(trade_date).toordinal() - 1))
        if date.fromisoformat(kline_start) > cutoff:
            raise ValueError("--kline-range must include at least one date before every trade date")
        kline_rows: dict[str, list[Sequence[Any]]] = {}
        missing_kline_tables = 0
        for symbol in symbols:
            if not re.fullmatch(r"\d{6}", symbol):
                continue
            try:
                cursor.execute(
                    f"SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg "
                    f"FROM d_{symbol} WHERE ts >= '{kline_start}' AND ts < '{trade_date}' "
                    f"AND ts <= '{cutoff.isoformat()} 23:59:59' ORDER BY ts"
                )
                kline_rows[symbol] = list(cursor.fetchall())
            except Exception as exc:
                if not _is_missing_table_error(exc):
                    raise
                missing_kline_tables += 1
        return snapshots, {
            "auction_row_count": auction_row_count,
            "missing_kline_table_count": missing_kline_tables,
            "kline_rows": kline_rows,
        }
    finally:
        connection.close()


def _prior_values(rows: Sequence[Sequence[Any]], trade_date: str) -> tuple[float | None, float | None]:
    cutoff = date.fromisoformat(trade_date)
    eligible: list[tuple[date, Sequence[Any]]] = []
    for row in rows:
        if not row:
            continue
        try:
            day = date.fromisoformat(str(row[0])[:10])
        except ValueError:
            continue
        if day < cutoff:
            eligible.append((day, row))
    if not eligible:
        return None, None
    _, row = max(eligible, key=lambda pair: pair[0])
    try:
        close = float(row[4]) if row[4] is not None else None
        amount = float(row[6]) if row[6] is not None else None
    except (IndexError, TypeError, ValueError, OverflowError):
        return None, None
    return close, amount


def build_day(trade_date: str, symbols: Sequence[str], memberships, hierarchy,
              snapshots: Mapping[str, Mapping[str, Any]], kline_rows: Mapping[str, Sequence[Sequence[Any]]],
              *, auction_row_count: int, missing_kline_table_count: int,
              snapshot_asof: str | None = None,
              snapshot_sealed_at: datetime | None = None) -> dict[str, Any]:
    stocks = []
    for symbol in symbols:
        pre_close, prev_amount = _prior_values(kline_rows.get(symbol, ()), trade_date)
        fact = derive_stock_auction_limit_fact(
            symbol=symbol, trade_date=trade_date, pre_close=pre_close,
            prev_day_amount=prev_amount, snapshots=snapshots.get(symbol, {}),
        )
        stocks.append(fact)
    stock_by_symbol = {fact.symbol: fact for fact in stocks}
    plates = aggregate_plate_auction_limit_facts(stock_by_symbol, memberships, hierarchy)
    with_previous = sum("NO_PRE_CLOSE" not in fact.flags for fact in stocks)
    with_auction = {
        tag: sum(any(value is not None for value in (
            getattr(fact, f"price_milli_{tag}"), getattr(fact, f"match_amt_{tag}"),
            getattr(fact, f"rest_bid_{tag}"), getattr(fact, f"rest_ask_{tag}"),
        )) for fact in stocks)
        for tag in ("0920", "0924", "0925")
    }
    with_0925_price = sum(fact.price_milli_0925 is not None for fact in stocks)
    point_in_time_by_stage = _point_in_time_flags(trade_date, snapshot_asof, snapshot_sealed_at)
    quality = {
        "trade_date": trade_date,
        "snapshot_asof": snapshot_asof,
        "snapshot_sealed_at": snapshot_sealed_at.isoformat() if snapshot_sealed_at else None,
        "point_in_time_by_stage": point_in_time_by_stage,
        "flags": ["NOT_POINT_IN_TIME"] if point_in_time_by_stage else [],
        "universe_symbol_count": len(symbols),
        "auction_row_count": auction_row_count,
        "missing_kline_table_count": missing_kline_table_count,
        "coverage": {
            "pre_close": with_previous / len(symbols) if symbols else None,
            **{f"auction_{tag}": count / len(symbols) if symbols else None
               for tag, count in with_auction.items()},
            "auction_0925_price": with_0925_price / len(symbols) if symbols else None,
        },
        "missing_counts": {
            "pre_close": len(stocks) - with_previous,
            "prev_day_amount": sum(fact.prev_day_amount is None for fact in stocks),
            **{f"auction_{tag}": len(stocks) - count for tag, count in with_auction.items()},
            "auction_0925_price": len(stocks) - with_0925_price,
        },
        "interpretation": "客观竞价事实统计, 非评分或策略结论",
    }
    return {"stocks": stocks, "plates": plates, "day_quality": quality}


def run(trade_dates: Sequence[str], snapshot_path: Path, kline_range: str,
        td_host: str, output_dir: Path, snapshot_asof: str | None = None) -> None:
    ensure_empty_output_dir(output_dir)
    snapshot = _load_snapshot(snapshot_path)
    snapshot_asof = _snapshot_asof(snapshot_path, snapshot_asof, snapshot)
    snapshot_sealed_at = _snapshot_sealed_at(snapshot_path, snapshot_asof, snapshot)
    symbols, memberships, hierarchy = _snapshot_memberships(snapshot)
    kline_start, kline_end = kline_range.split(":", 1)
    point_in_time_by_date: dict[str, list[str]] = {}
    for trade_date in trade_dates:
        snapshots, quality_inputs = _read_day(td_host, trade_date, kline_start, kline_end, symbols)
        result = build_day(
            trade_date, symbols, memberships, hierarchy, snapshots,
            quality_inputs.pop("kline_rows"), **quality_inputs,
            snapshot_asof=snapshot_asof, snapshot_sealed_at=snapshot_sealed_at,
        )
        folder = output_dir / trade_date
        folder.mkdir()
        _json_write(folder / "stocks.json", [stock_fact_to_dict(row) for row in result["stocks"]])
        _json_write(folder / "plates.json", [plate_fact_to_dict(row) for row in result["plates"]])
        _json_write(folder / "day_quality.json", result["day_quality"])
        point_in_time_by_date[trade_date] = result["day_quality"]["point_in_time_by_stage"]
    _json_write(output_dir / "manifest.json", {
        "trade_dates": list(trade_dates), "snapshot": str(snapshot_path),
        "snapshot_asof": snapshot_asof,
        "snapshot_sealed_at": snapshot_sealed_at.isoformat() if snapshot_sealed_at else None,
        "NOT_POINT_IN_TIME": point_in_time_by_date,
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, help="one or more comma-separated YYYY-MM-DD dates")
    parser.add_argument("--snapshot", type=Path, required=True, help="plate snapshot used by run_offline_plate_pipeline")
    parser.add_argument("--snapshot-asof", help="static snapshot date YYYY-MM-DD when its name does not include one")
    parser.add_argument("--kline-range", required=True, help="bounded TD range YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        date_parts = [part.strip() for part in args.trade_date.split(",")]
        dates = [date.fromisoformat(part).isoformat() for part in date_parts]
        kline_parts = args.kline_range.split(":", 1)
        if len(kline_parts) != 2 or any(date.fromisoformat(part).isoformat() != part
                                        for part in kline_parts + date_parts):
            raise ValueError
        if not dates or len(dates) != len(set(dates)):
            raise ValueError
        start, end = (date.fromisoformat(part) for part in kline_parts)
        if start > end or start >= min(date.fromisoformat(day) for day in dates):
            raise ValueError
    except ValueError:
        parser.error("dates must be unique ISO dates and --kline-range must be YYYY-MM-DD:YYYY-MM-DD before them")
    run(dates, args.snapshot, args.kline_range, args.td_host, args.output_dir, args.snapshot_asof)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

