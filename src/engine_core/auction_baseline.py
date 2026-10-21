"""Build PIT auction amount baselines from Core's read-only TD projection."""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .data_pipeline.storage import ArtifactCatalog, canonical_json

WINDOW_DAYS = 64
MARKET_SYMBOL = "__MARKET__"
SCHEMA = "auction_baseline_v3"


def _date(value: Any) -> str:
    raw = str(value or "")[:10].replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("invalid_trade_date_in_td")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _amount(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
            return None
        return int(parsed)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _percentile(values: Sequence[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _summary(values: Sequence[tuple[str, int]], *, window_dates: Sequence[str],
             missing_dates: Sequence[str]) -> dict[str, Any]:
    amounts = [amount for _, amount in values]
    return {
        "sample_days": len(amounts),
        "median_yuan": statistics.median(amounts) if amounts else None,
        "p10_yuan": _percentile(amounts, 0.10),
        "p25_yuan": _percentile(amounts, 0.25),
        "p75_yuan": _percentile(amounts, 0.75),
        "p90_yuan": _percentile(amounts, 0.90),
        "minimum_yuan": min(amounts) if amounts else None,
        "maximum_yuan": max(amounts) if amounts else None,
        "earliest_date": min((day for day, _ in values), default=None),
        "latest_date": max((day for day, _ in values), default=None),
        "window_start": window_dates[0] if window_dates else None,
        "window_end": window_dates[-1] if window_dates else None,
        "window_days": WINDOW_DAYS,
        "missing_dates": list(missing_dates),
    }


def build_auction_baseline(source_rows: Iterable[Mapping[str, Any]], *, target_date: str,
                           window_days: int = WINDOW_DAYS,
                           source_dates: Iterable[str] | None = None,
                           available_at: str | None = None,
                           fallback_market: Mapping[str, int] | None = None,
                           fallback_symbols: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Build daily market/symbol observations and rolling stats through D-1.

    A market total is emitted only when all returned final rows have amounts.
    Missing amounts remain missing; no values are filled with zero.
    """
    target = date.fromisoformat(target_date)
    if target.isoformat() != target_date or window_days != WINDOW_DAYS:
        raise ValueError("target_date_must_be_strict_and_window_must_be_64")
    cutoff = target.isoformat()
    source_rows = list(source_rows)
    source_dates = list(source_dates or (_date(row.get("trade_date")) for row in source_rows
                                         if row.get("trade_date") is not None))
    dates = sorted({_date(item) for item in source_dates if _date(item) < cutoff})
    selected_dates = dates[-window_days:]
    allowed = set(selected_dates)
    # Keep the latest 0925 projection per symbol/day if the source has revisions.
    latest: dict[tuple[str, str], tuple[str, int | None]] = {}
    source_by_symbol: dict[tuple[str, str], str] = {}
    for row in source_rows:
        if str(row.get("auction_tag") or "0925").strip() != "0925":
            continue
        day = _date(row.get("trade_date"))
        if day not in allowed or day >= cutoff:
            continue
        symbol = str(row.get("symbol") or "").strip()
        if symbol.isdigit() and len(symbol) <= 6:
            symbol = symbol.zfill(6)
        if not symbol:
            continue
        ts = str(row.get("ts") or "")
        key = (day, symbol)
        amount = _amount(row.get("match_amt_yuan", row.get("match_amount_yuan")))
        if key not in latest or ts >= latest[key][0]:
            latest[key] = (ts, amount)
            source_by_symbol[key] = "td_0925"

    for row in fallback_symbols:
        day = _date(row.get("trade_date", row.get("date")))
        symbol = str(row.get("symbol") or "").strip()
        if symbol.isdigit() and len(symbol) <= 6:
            symbol = symbol.zfill(6)
        amount = _amount(row.get("auction_amount_yuan"))
        key = (day, symbol)
        if day in allowed and day < cutoff and symbol and amount is not None and key not in latest:
            latest[key] = (str(row.get("available_at") or ""), amount)
            source_by_symbol[key] = "auction_points_rank"

    td_by_date: dict[str, list[tuple[str, int | None]]] = {day: [] for day in selected_dates}
    by_symbol: dict[str, list[tuple[str, int]]] = {}
    rows: list[dict[str, Any]] = []
    for (day, symbol), (ts, amount) in sorted(latest.items()):
        if source_by_symbol.get((day, symbol)) == "td_0925":
            td_by_date.setdefault(day, []).append((symbol, amount))
        if amount is not None:
            by_symbol.setdefault(symbol, []).append((day, amount))
            rows.append({"row_kind": "symbol_observation", "date": day, "symbol": symbol,
                         "auction_amount_yuan": amount, "source_tag": "0925",
                         "source": source_by_symbol.get((day, symbol), "td_0925"),
                         "source_ts": ts or None, "available_at": available_at})

    market_values: list[tuple[str, int]] = []
    market_missing: list[str] = []
    for day in selected_dates:
        observations = td_by_date.get(day, [])
        if observations and not any(value is None for _, value in observations):
            total = sum(int(value) for _, value in observations if value is not None)
            source = "td_0925"
        elif day in (fallback_market or {}):
            total = int(fallback_market[day])
            source = "auction_points_summary"
        else:
            market_missing.append(day)
            continue
        market_values.append((day, total))
        rows.append({"row_kind": "market_observation", "date": day,
                     "sum_yi": total / 100_000_000,
                     "auction_amount_yuan": total,
                     "symbol_count": len(observations) if source == "td_0925" else None,
                     "source_tag": "0925", "source": source, "available_at": available_at})

    market_summary = _summary(market_values, window_dates=selected_dates,
                              missing_dates=market_missing)
    rows.append({"row_kind": "market_baseline", "date": cutoff, "symbol": MARKET_SYMBOL,
                 **market_summary, "available_at": available_at})
    symbol_summaries: list[dict[str, Any]] = []
    for symbol, values in sorted(by_symbol.items()):
        seen = {day for day, _ in values}
        missing = [day for day in selected_dates if day not in seen]
        summary = {"row_kind": "symbol_baseline", "date": cutoff, "symbol": symbol,
                   **_summary(values, window_dates=selected_dates, missing_dates=missing),
                   "available_at": available_at}
        rows.append(summary)
        symbol_summaries.append(summary)

    status = "READY" if len(market_values) == len(selected_dates) and len(selected_dates) >= WINDOW_DAYS else "PARTIAL"
    if not market_values:
        status = "PARTIAL"
    return {"status": status, "target_date": cutoff,
            "point_in_time_date": selected_dates[-1] if selected_dates else None,
            "window_dates": selected_dates, "market_summary": market_summary,
            "symbol_summaries": symbol_summaries, "rows": rows}


def _td_connect():
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_required")
    try:
        import taos
    except ImportError as exc:
        raise RuntimeError("taospy_required_for_TD_read") from exc
    options: dict[str, Any] = {"database": "market_data1", "config": os.environ["TD_CONFIG"]}
    for env_name, option in (("TDENGINE_HOST", "host"), ("TDENGINE_PORT", "port"),
                             ("TDENGINE_USER", "user"), ("TDENGINE_PASSWORD", "password")):
        if os.environ.get(env_name):
            options[option] = int(os.environ[env_name]) if option == "port" else os.environ[env_name]
    return taos.connect(**options)


def _read_td_window(target_date: str) -> tuple[list[str], list[dict[str, Any]]]:
    cutoff = target_date.replace("-", "")
    connection = _td_connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT trade_date FROM market_data1.auction_snapshot_v2 "
                       f'WHERE trade_date < "{cutoff}" AND auction_tag="0925" '
                       "ORDER BY trade_date DESC LIMIT 64")
        dates = sorted({_date(row[0]) for row in cursor.fetchall()})
        if not dates:
            return [], []
        dates_sql = ",".join(f'"{day.replace("-", "")}"' for day in dates)
        cursor.execute("SELECT ts, symbol, trade_date, auction_tag, match_amt_yuan "
                       "FROM market_data1.auction_snapshot_v2 "
                       f"WHERE trade_date IN ({dates_sql}) AND auction_tag=\"0925\" "
                       "ORDER BY trade_date, symbol, ts")
        fetched = cursor.fetchall()
        names = ("ts", "symbol", "trade_date", "auction_tag", "match_amt_yuan")
        return dates, [dict(zip(names, row)) for row in fetched]
    finally:
        connection.close()


def _expected_sessions(catalog: ArtifactCatalog, target_date: str, td_dates: Sequence[str]) -> list[str]:
    from .calendar import CalendarCoverageError
    from .data_pipeline.trade_calendar import TradeCalendarStore
    try:
        store = TradeCalendarStore(catalog.root)
        from .trade_day_context import previous_trade_date as context_previous
        previous = context_previous(catalog.root, target_date)
        snapshot = store.load()
        if not snapshot:
            return []
        covered = [day for day in store.all_trading_dates()
                   if snapshot["lit_from"] <= day <= previous]
        if len(covered) < WINDOW_DAYS:
            raise CalendarCoverageError(
                f"calendar_history_insufficient:required={WINDOW_DAYS}:available={len(covered)}")
        return covered[-WINDOW_DAYS:]
    except CalendarCoverageError as exc:
        if str(exc).startswith("calendar_history_insufficient:"):
            raise
        return []


def _auction_points_fallback(catalog: ArtifactCatalog, dates: Sequence[str]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    market: dict[str, int] = {}
    symbols: list[dict[str, Any]] = []
    for day in dates:
        manifest_path = catalog.locate("auction_points", day)
        if manifest_path is None:
            continue
        for row in catalog.read_rows(manifest_path):
            if row.get("tag") != "0925":
                continue
            if row.get("row_kind") == "summary" and row.get("total_auction_amount_yuan") is not None:
                amount = _amount(row.get("total_auction_amount_yuan"))
                if amount is not None:
                    market[day] = amount
            elif row.get("row_kind") == "rank" and row.get("rank_list") == "top_amount":
                amount = _amount(row.get("auction_amount_yuan"))
                if amount is not None:
                    symbols.append({"trade_date": day, "symbol": row.get("symbol"),
                                    "auction_amount_yuan": amount,
                                    "available_at": row.get("captured_at")})
    return market, symbols


def _existing(catalog: ArtifactCatalog, target_date: str, *, asof: str | None = None
              ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    path = catalog.locate("auction_baseline", target_date)
    if path is None:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != SCHEMA or manifest.get("window_days") != WINDOW_DAYS:
            return None
        available = manifest.get("available_at")
        version = manifest.get("version")
        cutoff = asof or datetime.now(timezone.utc).isoformat()
        available_time = datetime.fromisoformat(str(available).replace("Z", "+00:00"))
        cutoff_time = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        indexed = any(item.get("version") == version for item in catalog.read().get(
            "datasets", {}).get("auction_baseline", {}).get("dates", {}).get(target_date, [])
                      if isinstance(item, dict))
        if (not isinstance(version, str) or not version or not indexed or
                available_time.tzinfo is None or cutoff_time.tzinfo is None or
                available_time > cutoff_time):
            return None
        return manifest, catalog.read_rows(path)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _publish_redis(target_date: str, manifest: Mapping[str, Any]) -> bool:
    url = os.environ.get("CORE_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        return False
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("redis_package_required_for_publish") from exc
    payload = canonical_json({"dataset": "auction_baseline", "trade_date": target_date,
                              "version": manifest.get("version"), "sha256": manifest.get("sha256"),
                              "available_at": manifest.get("available_at"),
                              "window": manifest.get("window"),
                              "market_summary": manifest.get("market_summary"),
                              "quality": manifest.get("quality")})
    if len(payload) > 65536:
        raise ValueError("auction_baseline_redis_payload_over_64KiB")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.set(f"core:cache:data:auction_baseline:{target_date}", payload.decode("utf-8"), ex=30 * 86400)
    return True


def build_and_store_auction_baseline(target_date: str, *, artifact_root: str | Path,
                                     publish: bool = True) -> dict[str, Any]:
    date.fromisoformat(target_date)
    catalog = ArtifactCatalog(artifact_root)
    old = _existing(catalog, target_date)
    if old:
        manifest, _ = old
        published = _publish_redis(target_date, manifest) if publish else False
        return {"status": manifest.get("business_status", "PARTIAL"), "reused": True,
                "row_count": manifest.get("row_count"), "version": manifest.get("version"),
                "sha256": manifest.get("sha256"), "available_at": manifest.get("available_at"),
                "market_summary": manifest.get("market_summary"), "redis_published": published}

    td_dates, source_rows = _read_td_window(target_date)
    source_dates = _expected_sessions(catalog, target_date, td_dates)
    fallback_market, fallback_symbols = _auction_points_fallback(catalog, source_dates)
    available_at = datetime.now(timezone.utc).isoformat()
    result = build_auction_baseline(source_rows, target_date=target_date,
                                    source_dates=source_dates, available_at=available_at,
                                    fallback_market=fallback_market,
                                    fallback_symbols=fallback_symbols)
    if not result["rows"]:
        raise RuntimeError("no_auction_snapshot_rows_before_target")
    manifest = {
        "schema": SCHEMA, "dataset": "auction_baseline", "trade_date": target_date,
        "as_of": result["point_in_time_date"], "point_in_time_date": result["point_in_time_date"],
        "available_at": available_at, "source": "market_data1.auction_snapshot_v2",
        "source_filter": "auction_tag=0925; match_amt_yuan final matched amount",
        "business_status": result["status"], "quality": {"status": result["status"],
            "missing": "INCOMPLETE_64_SESSION_WINDOW" if result["status"] == "PARTIAL" else None,
            "missing_market_dates": result["market_summary"]["missing_dates"],
            "observed_source_dates": len(result["window_dates"])},
        "window": {"sessions": WINDOW_DAYS, "from": result["market_summary"]["window_start"],
                   "through": result["market_summary"]["window_end"],
                   "excludes_target_date": True, "percentiles": "linear interpolation"},
        "window_days": WINDOW_DAYS, "market_summary": result["market_summary"],
        "symbol_count": len(result["symbol_summaries"]),
        "source_dates": result["window_dates"],
    }
    committed = catalog.commit("auction_baseline", target_date, result["rows"], manifest,
                               source="core_td_0925")
    published = _publish_redis(target_date, committed) if publish else False
    return {"status": result["status"], "reused": False, "row_count": committed["row_count"],
            "version": committed["version"], "sha256": committed["sha256"],
            "available_at": committed["available_at"], "market_summary": result["market_summary"],
            "redis_published": published}
