"""Core-owned daily history aggregation, indicators, and Baostock range import."""
from __future__ import annotations

import gzip
import json
import math
import multiprocessing
import os
import socket
from collections import defaultdict
from datetime import date, datetime, time as day_time, timezone
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .daily_kline_capture import parse_row, to_baostock_code
from .data_pipeline.storage import ArtifactCatalog, canonical_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
_RANGE_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg"
HOT_KLINE_DAYS = 5


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _mean(values: Sequence[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None) / len(values)


def _rsi(values: Sequence[float | None], period: int = 6) -> float | None:
    if len(values) < period + 1 or any(value is None for value in values):
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 0.0
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _limit_pct(symbol: str, stock_name: str | None) -> float:
    name = (stock_name or "").upper()
    if "ST" in name or "退" in name:
        return 4.8
    if symbol.startswith(("300", "301", "688", "689")):
        return 19.8
    if symbol.startswith(("8", "92")):
        return 29.8
    return 9.8


def calculate_daily_indicators(symbol: str, rows: Sequence[Mapping[str, Any]], *,
                               stock_name: str | None = None) -> dict[str, Any]:
    """Calculate verified legacy candidate formulas; unavailable inputs stay null."""
    ordered = sorted((dict(row) for row in rows if str(row.get("symbol", "")).zfill(6) == symbol),
                     key=lambda row: str(row.get("date", row.get("time", "")))[:10])
    closes = [_number(row.get("close")) for row in ordered]
    turnovers = [_number(row.get("turnover_pct", row.get("turn"))) for row in ordered]
    changes = [_number(row.get("pct_chg", row.get("change_pct"))) for row in ordered]
    output: dict[str, Any] = {
        "symbol": symbol,
        "date": str(ordered[-1].get("date", ordered[-1].get("time", "")))[:10] if ordered else None,
        "source": "core_derived",
        "field_units": {"bias_20": "percent_points", "rsi_6": "index_0_100",
                         "change_pct_5d": "percent_points", "avg_turnover_5d": "UNVERIFIED",
                         "limit_up_days_5": "legacy_threshold_heuristic_UNVERIFIED",
                         "vol_ratio": "dimensionless_legacy_formula"},
    }
    for window in (5, 10, 20):
        avg = _mean(closes[-window:]) if len(closes) >= window else None
        output[f"ma{window}"] = _rounded(avg)
    close = closes[-1] if closes else None
    ma20 = output["ma20"]
    output["bias_20"] = _rounded((close - ma20) / ma20 * 100.0) if close is not None and ma20 not in (None, 0) else None
    output["rsi_6"] = _rounded(_rsi(closes, 6))
    reference = closes[-6] if len(closes) >= 6 else None
    output["change_pct_5d"] = _rounded((close / reference - 1.0) * 100.0) if close is not None and reference not in (None, 0) else None
    last_five_turns = turnovers[-5:] if len(turnovers) >= 5 else []
    avg_turn = _mean(last_five_turns)
    output["avg_turnover_5d"] = _rounded(avg_turn)
    last_five_changes = changes[-5:] if len(changes) >= 5 else []
    threshold = _limit_pct(symbol, stock_name)
    output["limit_up_days_5"] = sum(1 for value in last_five_changes if value is not None and value > threshold) if len(last_five_changes) == 5 and all(v is not None for v in last_five_changes) else None
    baseline = turnovers[-20:-5] if len(turnovers) >= 20 else []
    base_turn = _mean(baseline)
    output["vol_ratio"] = _rounded(avg_turn / (base_turn + 0.1)) if avg_turn is not None and base_turn is not None and base_turn + 0.1 != 0 else None
    return output


def core_history_rows(catalog: ArtifactCatalog, *, through_date: str, asof: str,
                      max_days: int = 120) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read bounded daily_kline versions and fail closed on unavailable/future days."""
    cutoff = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("slice_asof_timezone_required")
    catalog_snapshot = catalog.read()
    date_versions = catalog_snapshot.get("datasets", {}).get("daily_kline", {}).get("dates", {})
    available_dates = sorted(day for day in date_versions
                             if day <= through_date)[-max_days:]
    rows: list[dict[str, Any]] = []
    sources: set[str] = set()
    unknown_availability = False
    max_available: datetime | None = None
    included_dates: list[str] = []
    for day in available_dates:
        versions = date_versions.get(day, [])
        path = Path(versions[-1]["manifest"]) if versions else None
        if path is None or not path.is_file():
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        available_text = manifest.get("available_at")
        try:
            available = datetime.fromisoformat(str(available_text).replace("Z", "+00:00"))
            if available.tzinfo is None:
                unknown_availability = True
                continue
        except (TypeError, ValueError):
            unknown_availability = True
            continue
        if available > cutoff:
            continue
        if max_available is None or available > max_available:
            max_available = available
        source = str(manifest.get("source") or "unknown")
        sources.add(source)
        day_rows = catalog.read_rows(path)
        seen: set[str] = set()
        for raw in day_rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or row.get("code") or "").zfill(6)
            event_date = str(row.get("date") or row.get("time") or day)[:10]
            if not symbol.isdigit() or len(symbol) != 6 or event_date > through_date:
                continue
            row.update({"symbol": symbol, "date": event_date, "source": source,
                        "available_at": available.isoformat()})
            rows.append(row)
            seen.add(symbol)
        if seen:
            included_dates.append(day)
    rows.sort(key=lambda row: (row["symbol"], row["date"]))
    metadata = {"available_at": max_available.isoformat() if max_available else None,
                "source": "core_data" if rows else "core_data", "sources": sorted(sources),
                "history_trade_dates": len(included_dates), "row_count": len(rows),
                "unknown_availability": unknown_availability,
                "field_units": {"turnover_pct": "UNVERIFIED"}}
    return rows, metadata


def build_indicator_rows(rows: Sequence[Mapping[str, Any]], *, trade_date: str,
                         names: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol", "")).zfill(6)
        if symbol.isdigit() and str(row.get("date", ""))[:10] <= trade_date:
            grouped[symbol].append(row)
    output = []
    for symbol, history in sorted(grouped.items()):
        values = calculate_daily_indicators(symbol, history, stock_name=(names or {}).get(symbol))
        if values["date"] != trade_date:
            continue
        output.append(values)
    return output


def build_daily_report_features(rows: Sequence[Mapping[str, Any]], *, trade_date: str) -> list[dict[str, Any]]:
    """Materialize report-only rolling context from PIT-eligible daily bars."""
    eligible = [dict(row) for row in rows if str(row.get("date") or row.get("time") or "")[:10] <= trade_date]
    symbols = sorted({str(row.get("symbol") or row.get("code") or "").zfill(6)
                      for row in eligible if str(row.get("symbol") or row.get("code") or "").isdigit()})
    # Reuse the established report formulas for streaks, positions and the
    # 72-session market opening baseline.
    from .live_session import daily_history_context
    states = {symbol: {} for symbol in symbols}
    context = daily_history_context(eligible, f"{trade_date}T23:59:59", states)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_amount_days = sorted({str(row.get("date") or row.get("time") or "")[:10]
                              for row in eligible if _number(row.get("amount_yuan", row.get("amount"))) is not None})[-64:]
    for row in eligible:
        symbol = str(row.get("symbol") or row.get("code") or "").zfill(6)
        if symbol in states:
            by_symbol[symbol].append(row)
    result: list[dict[str, Any]] = []
    for symbol in symbols:
        history = sorted(by_symbol[symbol], key=lambda row: str(row.get("date") or row.get("time") or "")[:10])
        closes = [_number(row.get("close")) for row in history]
        close_values = [value for value in closes if value is not None]
        position = context["stocks"].get(symbol, {})
        amounts_by_day: dict[str, float] = {}
        for row in history:
            day = str(row.get("date") or row.get("time") or "")[:10]
            amount = _number(row.get("amount_yuan", row.get("amount")))
            if day and amount is not None:
                amounts_by_day[day] = amount
        amount_history = ([amounts_by_day[day] for day in all_amount_days]
                          if len(all_amount_days) == 64 and all(day in amounts_by_day for day in all_amount_days)
                          else None)
        complete_position = len(close_values) >= 20
        close_window = close_values[-20:] if complete_position else []
        result.append({"feature_type": "daily_report_features_v1", "trade_date": trade_date,
                       "date": trade_date, "symbol": symbol,
                       "last_history_date": position.get("last_history_date"),
                       "streak": position.get("streak"),
                       "position_20d": position.get("position_20d"),
                       "position_close_days": position.get("position_close_days", 0),
                       "range20_low": min(close_window) if complete_position else None,
                       "range20_high": max(close_window) if complete_position else None,
                       "range20_last_close": close_window[-1] if complete_position else None,
                       "prev_day_amount_yuan": position.get("prev_day_amount_yuan"),
                       "amount_history_64": amount_history})
    market = {"feature_type": "daily_report_features_v1", "trade_date": trade_date,
              "date": trade_date, "symbol": "__MARKET__",
              "opening_history_72": context["history"].get("daily", []),
              "amount_history_dates_64": all_amount_days if len(all_amount_days) == 64 else None}
    result.append(market)
    return result


def materialize_daily_report_features(catalog: ArtifactCatalog, *, trade_date: str, asof: str,
                                      redis: Any = None) -> dict[str, Any]:
    """Read canonical history, persist immutable features, and publish a compressed hot view."""
    history, history_meta = core_history_rows(catalog, through_date=trade_date, asof=asof, max_days=120)
    rows = build_daily_report_features(history, trade_date=trade_date)
    available_at = history_meta.get("available_at")
    manifest = catalog.commit("daily_report_features", trade_date, rows,
                              {"dataset": "daily_report_features", "source": "core_data",
                               "available_at": available_at, "as_of": asof,
                               "quality": {"row_count": len(rows), "history_trade_dates": history_meta.get("history_trade_dates")},
                               "dependencies": ["daily_kline"]}, source="core_derived")
    cache = {"schema_version": 1, "dataset": "daily_report_features", "trade_date": trade_date,
             "source": "core_data", "available_at": available_at, "row_count": len(rows), "rows": rows}
    encoded = gzip.compress(canonical_json(cache), mtime=0)
    if redis is not None:
        redis.set(f"core:cache:data:daily_report_features:{trade_date}", encoded, ex=7 * 86400)
        meta = {key: cache[key] for key in ("schema_version", "dataset", "trade_date", "source", "available_at", "row_count")}
        redis.set(f"core:cache:meta:daily_report_features:{trade_date}", canonical_json(meta), ex=7 * 86400)
        readback = redis.get(f"core:cache:data:daily_report_features:{trade_date}")
        if gzip.decompress(readback) != canonical_json(cache):
            raise RuntimeError("daily_report_features_redis_readback_mismatch")
    return {"manifest": manifest, "row_count": len(rows), "available_at": available_at,
            "history_trade_dates": history_meta.get("history_trade_dates"),
            "cache_bytes": len(encoded) if redis is not None else None}


def write_daily_history_cache(redis: Any, trade_date: str, rows: Sequence[Mapping[str, Any]], *,
                              available_at: str | None, source: str = "core_data") -> bool:
    """Publish five recent bars per symbol plus latest-day indicator features."""
    if redis is None:
        return False
    eligible = [dict(row) for row in rows if str(row.get("date") or "")[:10] <= trade_date]
    dates = sorted({str(row.get("date") or "")[:10] for row in eligible if row.get("date")})
    hot_dates = set(dates[-HOT_KLINE_DAYS:])
    hot_rows = [row for row in eligible if str(row.get("date") or "")[:10] in hot_dates]
    payload = {"schema_version": 2, "dataset": "daily_history", "trade_date": trade_date,
               "available_at": available_at, "source": source, "hot_window_days": HOT_KLINE_DAYS,
               "row_count": len(hot_rows), "rows": hot_rows}
    key = f"core:cache:data:daily_history:{trade_date}"
    try:
        redis.set(key, gzip.compress(canonical_json(payload), mtime=0), ex=7 * 86400)
        return True
    except Exception:
        return False


def read_daily_history_cache(redis: Any, trade_date: str, *, asof: str,
                              history_days: int = HOT_KLINE_DAYS) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if history_days > HOT_KLINE_DAYS:
        return None
    if redis is None or not callable(getattr(redis, "get", None)):
        return None
    try:
        raw = redis.get(f"core:cache:data:daily_history:{trade_date}")
        if not raw:
            return None
        payload = json.loads(gzip.decompress(raw))
        if (payload.get("schema_version") != 2 or payload.get("hot_window_days") != HOT_KLINE_DAYS
                or payload.get("dataset") != "daily_history" or payload.get("trade_date") != trade_date
                or payload.get("source") != "core_data" or payload.get("row_count") != len(payload.get("rows", []))):
            return None
        cutoff = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            return None
        rows = []
        for raw_row in payload["rows"]:
            row = dict(raw_row)
            try:
                available = datetime.fromisoformat(str(row.get("available_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if available.tzinfo is not None and available <= cutoff and str(row.get("date", ""))[:10] <= trade_date:
                rows.append(row)
        if not rows:
            return None
        latest_available = max(row["available_at"] for row in rows)
        return rows, {"available_at": latest_available, "source": "core_data",
                      "row_count": len(rows), "history_trade_dates": len({row.get("date") for row in rows}),
                      "unknown_availability": False, "sources": sorted({str(row.get("source") or "unknown") for row in rows}),
                      "field_units": {"turnover_pct": "UNVERIFIED"}}
    except Exception:
        # The Redis hot view is optional; an unavailable cache must fall through
        # to the immutable Core artifact path.
        return None


class BaostockRangeClient:
    """Baostock interval reader with socket timeout and one reconnect retry."""
    def __init__(self, module: Any, *, read_timeout: float = 30.0):
        if read_timeout <= 0:
            raise ValueError("read_timeout_must_be_positive")
        self.module = module
        self.read_timeout = read_timeout

    def login(self) -> None:
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.read_timeout)
        try:
            result = self.module.login()
        finally:
            socket.setdefaulttimeout(previous_timeout)
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError("baostock_login_failed")
        from baostock.common import context
        sock = getattr(context, "default_socket", None)
        if sock is None:
            raise RuntimeError("baostock_socket_missing")
        sock.settimeout(self.read_timeout)

    def reconnect(self) -> None:
        from baostock.common import context
        sock = getattr(context, "default_socket", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        setattr(context, "default_socket", None)
        self.login()

    def query(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        code = to_baostock_code(symbol)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result = self.module.query_history_k_data_plus(
                    code, _RANGE_FIELDS, start_date=start, end_date=end,
                    frequency="d", adjustflag="3")
                if getattr(result, "error_code", "0") != "0":
                    raise RuntimeError("baostock_query_failed")
                rows = []
                while result.next():
                    raw = dict(zip(result.fields, result.get_row_data()))
                    row = parse_row(raw)
                    if row["symbol"] != symbol or not start <= row["date"] <= end:
                        raise ValueError("baostock_range_identity_mismatch")
                    row["pre_close"] = parse_row({**raw, "pre_close": raw.get("preclose")})["pre_close"]
                    rows.append(row)
                if getattr(result, "error_code", "0") != "0":
                    raise RuntimeError("baostock_page_failed")
                return rows
            except Exception as exc:
                last_error = exc
                if isinstance(exc, (TimeoutError, socket.timeout)):
                    raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                if isinstance(exc, (ConnectionError, OSError)):
                    raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from exc
                if attempt:
                    break
                self.reconnect()
        raise RuntimeError(f"baostock_range_failed:{type(last_error).__name__}")


def _symbols_from_core(catalog: ArtifactCatalog) -> list[str]:
    latest = (catalog.read().get("datasets", {}).get("daily_kline", {}).get("latest") or {}).get("trade_date")
    if not latest:
        raise RuntimeError("daily_kline_symbol_universe_missing")
    manifest = catalog.locate("daily_kline", latest)
    if manifest is None:
        raise RuntimeError("daily_kline_symbol_manifest_missing")
    symbols = sorted({str(row.get("symbol") or row.get("code") or "").zfill(6)
                      for row in catalog.read_rows(manifest)
                      if str(row.get("symbol") or row.get("code") or "").zfill(6).isdigit()})
    if not symbols:
        raise RuntimeError("daily_kline_symbol_universe_empty")
    return symbols


def _fetch_worker(symbols: list[str], start: str, end: str, checkpoint: str,
                  batch_size: int, read_timeout: float) -> int:
    """Process-isolated Baostock connection; module-global sockets stay private."""
    import baostock as bs
    folder = Path(checkpoint)
    folder.mkdir(parents=True, exist_ok=True)
    completed: set[str] = set()
    for part in folder.glob("symbols-*.json.gz"):
        with gzip.open(part, "rt", encoding="utf-8") as handle:
            completed.update(json.load(handle).get("symbols", []))
    todo = [symbol for symbol in symbols if symbol not in completed]
    client = BaostockRangeClient(bs, read_timeout=read_timeout)
    client.login()
    try:
        for offset in range(0, len(todo), batch_size):
            group = todo[offset:offset + batch_size]
            batch_rows = []
            for symbol in group:
                batch_rows.extend(client.query(symbol, start, end))
            payload = {"source": "baostock", "symbols": group,
                       "parameters": {"start": start, "end": end,
                        "frequency": "d", "adjustflag": "3", "symbols": group},
                       "fetched_at": datetime.now(timezone.utc).isoformat(), "rows": batch_rows}
            part = folder / f"symbols-{len(completed):06d}-{len(completed) + len(group):06d}.json.gz"
            temporary = part.with_suffix(part.suffix + ".pending")
            temporary.write_bytes(gzip.compress(canonical_json(payload), mtime=0))
            os.replace(temporary, part)
            completed.update(group)
    finally:
        bs.logout()
    return len(completed)


def backfill_daily_history(catalog: ArtifactCatalog, *, start: str, end: str,
                           checkpoint_root: str | Path, client: Any,
                           batch_size: int = 50, redis: Any = None, workers: int = 1,
                           read_timeout: float = 30.0) -> dict[str, Any]:
    """Fetch one interval per symbol and checkpoint completed symbol batches."""
    date.fromisoformat(start); date.fromisoformat(end)
    if start > end or batch_size < 1 or not 1 <= workers <= 4:
        raise ValueError("daily_history_backfill_range_invalid")
    symbols = _symbols_from_core(catalog)
    # A completed-symbol checkpoint is only valid for the exact requested
    # interval. Keep ranges isolated so a later wider backfill cannot inherit
    # a narrower run's completion markers.
    checkpoint = Path(checkpoint_root) / f"range-{start}-{end}"
    checkpoint.mkdir(parents=True, exist_ok=True)
    fetched: list[dict[str, Any]] = []
    completed: set[str] = set()
    for part in sorted(checkpoint.rglob("symbols-*.json.gz")):
        with gzip.open(part, "rt", encoding="utf-8") as handle:
            saved = json.load(handle)
        fetched.extend(saved.get("rows", []))
        completed.update(saved.get("symbols", []))
    missing = [symbol for symbol in symbols if symbol not in completed]
    if workers == 1:
        client.login()
        try:
            for offset in range(0, len(missing), batch_size):
                group = missing[offset:offset + batch_size]
                batch_rows: list[dict[str, Any]] = []
                for symbol in group:
                    batch_rows.extend(client.query(symbol, start, end))
                payload = {"source": "baostock", "symbols": group,
                           "parameters": {"start": start, "end": end,
                            "frequency": "d", "adjustflag": "3", "symbols": group},
                           "fetched_at": datetime.now(timezone.utc).isoformat(), "rows": batch_rows}
                part = checkpoint / f"symbols-{len(completed):06d}-{len(completed) + len(group):06d}.json.gz"
                temporary = part.with_suffix(part.suffix + ".pending")
                temporary.write_bytes(gzip.compress(canonical_json(payload), mtime=0))
                os.replace(temporary, part)
                fetched.extend(batch_rows)
                completed.update(group)
        finally:
            client.module.logout() if hasattr(client, "module") else client.logout()
    elif missing:
        groups = [missing[index::workers] for index in range(workers)]
        with ProcessPoolExecutor(max_workers=workers,
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(_fetch_worker, group, start, end,
                                   str(checkpoint / f"worker-{index:02d}"),
                                   batch_size, read_timeout)
                       for index, group in enumerate(groups) if group]
            for future in futures:
                future.result()
        fetched = []
        completed = set()
        for part in sorted(checkpoint.rglob("symbols-*.json.gz")):
            with gzip.open(part, "rt", encoding="utf-8") as handle:
                saved = json.load(handle)
            fetched.extend(saved.get("rows", []))
            completed.update(saved.get("symbols", []))
    by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in fetched:
        by_day[str(row["date"])][str(row["symbol"]).zfill(6)] = dict(row)
    committed = []
    for day, day_rows in sorted(by_day.items()):
        available = datetime.combine(date.fromisoformat(day), day_time(17, 30), SHANGHAI)
        for row in day_rows.values():
            row.update({"source": "baostock", "available_at": available.isoformat()})
        manifest = {"schema_version": 1, "dataset": "daily_kline", "trade_date": day,
                    "source": "baostock", "available_at": available.isoformat(),
                    "as_of": datetime.now(SHANGHAI).isoformat(), "status": "FORMAL",
                    "field_units": {"amount_yuan": "CNY", "turnover_pct": "UNVERIFIED"},
                    "row_count": len(day_rows), "backfill_range": [start, end]}
        committed.append(catalog.commit("daily_kline", day, sorted(day_rows.values(), key=lambda row: row["symbol"]),
                                        manifest, source="baostock"))
        history, _meta = core_history_rows(catalog, through_date=day, asof=available.isoformat(), max_days=120)
        indicators = build_indicator_rows(history, trade_date=day)
        catalog.commit("daily_indicators", day, indicators,
                       {"schema_version": 1, "dataset": "daily_indicators", "trade_date": day,
                        "source": "core_derived", "available_at": available.isoformat(),
                        "field_units": {"avg_turnover_5d": "UNVERIFIED"}, "row_count": len(indicators)},
                       source="core_derived")
    cache_rows, cache_meta = core_history_rows(catalog, through_date=end,
                                               asof=datetime.combine(date.fromisoformat(end), day_time(23, 59), SHANGHAI).isoformat(),
                                               max_days=120)
    indicator_manifest = catalog.locate("daily_indicators", end)
    if indicator_manifest is not None and indicator_manifest.is_file():
        latest_indicators = {str(row.get("symbol", "")).zfill(6): row
                             for row in catalog.read_rows(indicator_manifest)}
        for row in cache_rows:
            if row.get("date") != end:
                continue
            values = latest_indicators.get(str(row.get("symbol", "")).zfill(6))
            if values:
                for field in ("ma5", "ma10", "ma20", "bias_20", "rsi_6", "change_pct_5d",
                              "avg_turnover_5d", "limit_up_days_5", "vol_ratio"):
                    row[field] = values.get(field)
                row["indicator_source"] = "core_derived"
                row["indicator_available_at"] = cache_meta.get("available_at")
    cache_written = write_daily_history_cache(redis, end, cache_rows,
                                              available_at=cache_meta.get("available_at"))
    return {"start": start, "end": end, "symbol_count": len(symbols),
            "completed_symbols": len(completed), "rows": len(fetched),
            "days_committed": len(committed), "cache_rows": len(cache_rows),
            "redis_hot_view_written": cache_written, "checkpoint_root": str(checkpoint)}


def reindex_daily_indicators(catalog: ArtifactCatalog, *, start: str, end: str, redis: Any = None) -> dict[str, Any]:
    """Recompute indicators from stored daily_kline versions (no network); appends new indicator versions and refreshes the hot view."""
    days = sorted(day for day in catalog.read().get("datasets", {}).get("daily_kline", {}).get("dates", {})
                  if start <= day <= end)
    for day in days:
        available = datetime.combine(date.fromisoformat(day), day_time(17, 30), SHANGHAI)
        kline_manifest = catalog.locate("daily_kline", day)
        if kline_manifest is None or not kline_manifest.is_file():
            raise RuntimeError(f"daily_kline_manifest_missing:{day}")
        # Indicators cannot be visible before the daily_kline they derive from (kline may land after 17:30).
        try:
            kline_meta = json.loads(kline_manifest.read_text(encoding="utf-8"))
            kline_at = datetime.fromisoformat(str(kline_meta.get("available_at", "")).replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"daily_kline_manifest_unreadable:{day}:{type(exc).__name__}") from exc
        from .guardian.registry import kline_coverage_problem
        problem = kline_coverage_problem(kline_meta.get("quality"))
        if problem:
            raise RuntimeError(f"{problem}:{day}")
        if kline_at.tzinfo is not None and kline_at.astimezone(SHANGHAI) > available:
            available = kline_at.astimezone(SHANGHAI)
        history, _meta = core_history_rows(catalog, through_date=day, asof=available.isoformat(), max_days=120)
        indicators = build_indicator_rows(history, trade_date=day)
        if not indicators:
            raise RuntimeError(f"daily_indicators_empty:{day}")
        catalog.commit("daily_indicators", day, indicators,
                       {"schema_version": 1, "dataset": "daily_indicators", "trade_date": day,
                        "source": "core_derived", "available_at": available.isoformat(),
                        "field_units": {"avg_turnover_5d": "UNVERIFIED"}, "row_count": len(indicators),
                        "reindex": True},
                       source="core_derived")
    cache_rows, cache_meta = core_history_rows(catalog, through_date=end,
                                               asof=datetime.combine(date.fromisoformat(end), day_time(23, 59), SHANGHAI).isoformat(),
                                               max_days=120)
    manifest = catalog.locate("daily_indicators", end)
    if manifest is not None and manifest.is_file():
        latest = {str(row.get("symbol", "")).zfill(6): row for row in catalog.read_rows(manifest)}
        for row in cache_rows:
            values = latest.get(str(row.get("symbol", "")).zfill(6)) if row.get("date") == end else None
            if values:
                for field in ("ma5", "ma10", "ma20", "bias_20", "rsi_6", "change_pct_5d",
                              "avg_turnover_5d", "limit_up_days_5", "vol_ratio"):
                    row[field] = values.get(field)
                row["indicator_source"] = "core_derived"
                row["indicator_available_at"] = cache_meta.get("available_at")
    written = write_daily_history_cache(redis, end, cache_rows, available_at=cache_meta.get("available_at"))
    return {"days_reindexed": len(days), "cache_rows": len(cache_rows), "redis_hot_view_written": written}
