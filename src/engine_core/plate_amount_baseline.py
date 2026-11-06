"""Point-in-time plate amount baselines at 09:25 and 09:45."""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .data_pipeline.storage import ArtifactCatalog, canonical_json
from .data_pipeline.td_sink import td_memory_exhausted, td_statement_error

DATASET = "plate_amount_baseline"
SCHEMA = "plate_amount_baseline_v1"
WINDOW_DAYS = 64
SLOT_0945_WINDOW_START = "09:30:01"
SLICE_DAYS = 5
SLOTS = ("0925", "0945")
# A finished day's 09:25 and 09:45 amounts never change, so each is read from TD once and kept in Redis. Reading the
# whole window every morning cost 262 s on an idle TD with 28 sessions and grows by ~9 s per session up to WINDOW_DAYS.
OBS_KEY_PREFIX = "core:plate_amount_obs:v1:"
OBS_TTL_SECONDS = 100 * 86400
MIN_FINAL_SYMBOLS = 4000        # fewer observations in a slot: the day was not captured whole, so it is never kept


def _day(value: Any) -> str:
    raw = str(value or "")[:10]
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return date.fromisoformat(raw).isoformat()


def _symbol(value: Any) -> str:
    raw = str(value or "").strip()
    return raw.zfill(6) if raw.isdigit() and len(raw) <= 6 else raw


def _percentile(values: Sequence[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return float(ordered[low]) if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_plate_amount_baseline(*, target_date: str,
                                membership_by_date: Mapping[str, Iterable[Mapping[str, Any]]],
                                observations: Iterable[Mapping[str, Any]],
                                candidate_dates: Iterable[str],
                                membership_versions: Mapping[str, str] | None = None,
                                available_at: str | None = None) -> dict[str, Any]:
    target = _day(target_date)
    dates = sorted({_day(item) for item in candidate_dates if _day(item) < target})[-WINDOW_DAYS:]
    membership: dict[str, dict[str, set[str]]] = {}
    plates: set[str] = set()
    for raw_day, rows in membership_by_date.items():
        day = _day(raw_day)
        by_plate: dict[str, set[str]] = {}
        for row in rows:
            code = str(row.get("plate_code", row.get("plateid", row.get("code_plate", row.get("plate", "")))) or "").strip()
            symbol = _symbol(row.get("symbol", row.get("stock_code", row.get("stockid", row.get("code", "")))))
            if code and symbol:
                by_plate.setdefault(code, set()).add(symbol)
        membership[day] = by_plate
        plates.update(by_plate)
    amounts: dict[tuple[str, str, str], int] = {}
    for row in observations:
        day = _day(row.get("trade_date", row.get("date")))
        slot = str(row.get("slot") or "")
        symbol = _symbol(row.get("symbol"))
        try:
            amount = int(row.get("amount_yuan"))
        except (TypeError, ValueError, OverflowError):
            continue
        if day in dates and slot in SLOTS and symbol and amount >= 0:
            amounts[(day, slot, symbol)] = amount
    result_rows: list[dict[str, Any]] = []
    for code in sorted(plates):
        for slot in SLOTS:
            values: list[tuple[str, int]] = []
            missing: list[str] = []
            for day in dates:
                members = membership.get(day, {}).get(code)
                if not members:
                    missing.append(day)
                    continue
                facts = [amounts.get((day, slot, symbol)) for symbol in members]
                if any(value is None for value in facts):
                    missing.append(day)
                    continue
                values.append((day, sum(int(value) for value in facts if value is not None)))
            vals = [value for _, value in values]
            result_rows.append({
                "row_kind": "plate_baseline", "trade_date": target, "plate_code": code,
                "slot": slot, "median_yuan": statistics.median(vals) if vals else None,
                "p25_yuan": _percentile(vals, .25), "p75_yuan": _percentile(vals, .75),
                "sample_days": len(vals), "amounts_yuan": vals,
                "dates_used": [day for day, _ in values], "missing_dates": missing,
                "membership_versions": {day: (membership_versions or {}).get(day) for day, _ in values},
                "available_at": available_at,
            })
    return {"status": "READY" if result_rows and all(not row["missing_dates"] for row in result_rows) else "PARTIAL",
            "target_date": target, "window_dates": dates, "rows": result_rows}


def _td_connect():
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_required")
    from .data_pipeline.td_sink import connect_td
    return connect_td("market_data1")


SYMBOL_SPLITS = (("000000", "300000"), ("300000", "600000"), ("600000", "999999"))


def _query_error(exc: Exception, reason: str) -> Exception:
    try:
        from taos.error import Error as TdDriverError
    except ImportError:
        TdDriverError = ()
    if isinstance(exc, TdDriverError):
        return td_statement_error(exc, reason)
    return RuntimeError(reason)


def _read_0945_slice(cursor: Any, days: Sequence[str], wanted: set[str],
                     symbols: tuple[str, str] | None = None) -> list[dict[str, Any]]:
    first, last = min(days), max(days)
    symbol_filter = f' AND symbol >= "{symbols[0]}" AND symbol < "{symbols[1]}"' if symbols else ""
    try:
        cursor.execute("SELECT _wstart, symbol, LAST(amt_yuan) FROM market_data1.stock_tick_v2 "
                       f'WHERE ts >= "{first} 09:30:00" AND ts <= "{last} 09:45:00"{symbol_filter} '
                       "PARTITION BY symbol INTERVAL(15m, 1s)")
        fetched = cursor.fetchall()
    except Exception as exc:
        if td_memory_exhausted(exc):
            if len(days) > 1:
                middle = len(days) // 2
                return (_read_0945_slice(cursor, days[:middle], wanted, symbols)
                        + _read_0945_slice(cursor, days[middle:], wanted, symbols))
            if symbols is None:  # one day still too big for the shared TDengine: split the symbols
                return [row for part in SYMBOL_SPLITS
                        for row in _read_0945_slice(cursor, days, wanted, part)]
        raise _query_error(exc, f"plate_amount_baseline_tick_slice {first}..{last}"
                           f"{' ' + symbols[0] + '-' + symbols[1] if symbols else ''}: {exc}") from exc
    out = []
    for window_start, symbol, amount in fetched:
        stamp = str(window_start)
        if stamp[11:19] != SLOT_0945_WINDOW_START or stamp[:10] not in wanted or amount is None:
            continue
        out.append({"trade_date": stamp[:10], "slot": "0945", "symbol": _symbol(symbol), "amount_yuan": amount})
    return out


def _read_auction_rows(cursor: Any, days: Sequence[str]) -> list[tuple]:
    """09:25 auction snapshots of ``days``; SLICE_DAYS per query, a slice that runs out of query memory is halved.
    No ORDER BY: the caller keeps the latest snapshot per (day, symbol) itself, and sorting is what costs memory."""
    rows: list[tuple] = []
    for index in range(0, len(days), SLICE_DAYS):
        rows.extend(_auction_slice(cursor, list(days[index:index + SLICE_DAYS])))
    return rows


def _auction_slice(cursor: Any, days: list[str]) -> list[tuple]:
    quoted = ",".join(f'"{day.replace("-", "")}"' for day in days)
    try:
        cursor.execute("SELECT ts, symbol, trade_date, auction_tag, match_amt_yuan "
                       "FROM market_data1.auction_snapshot_v2 "
                       f'WHERE trade_date IN ({quoted}) AND auction_tag="0925"')
        return list(cursor.fetchall())
    except Exception as exc:
        if td_memory_exhausted(exc) and len(days) > 1:
            middle = len(days) // 2
            return _auction_slice(cursor, days[:middle]) + _auction_slice(cursor, days[middle:])
        raise _query_error(exc, f"plate_amount_baseline_auction_query {days[0]}..{days[-1]}: {exc}") from exc


def _read_days(dates: Sequence[str]) -> list[dict[str, Any]]:
    """The observations of ``dates`` straight from TD (sliced queries)."""
    connection = _td_connect()
    result: list[dict[str, Any]] = []
    try:
        cursor = connection.cursor()
        auction_rows = _read_auction_rows(cursor, list(dates))
        latest: dict[tuple[str, str], tuple[str, Any]] = {}
        for ts, symbol, day, _tag, amount in auction_rows:
            key = (_day(day), _symbol(symbol))
            stamp = str(ts)
            if key not in latest or stamp >= latest[key][0]:
                latest[key] = (stamp, amount)
        result.extend({"trade_date": day, "slot": "0925", "symbol": symbol, "amount_yuan": amount}
                      for (day, symbol), (_stamp, amount) in latest.items() if amount is not None)
        # Sliced queries, not one scan per trading day and not one query for the whole window. amt_yuan is the day's
        # cumulative amount, so the last tick of each 15-minute slice is the cumulative value at the slice end. Slices
        # are shifted by 1 s ([09:30:01, 09:45:01)), which makes the 09:30:01 slice end exactly at the 09:45:00 tick:
        # the value the old "ts <= 09:45:00" query returned (identical for every symbol on all 24 days checked
        # 2026-11-02). Memory grows with symbols x windows, so a slice covers SLICE_DAYS trading days (about 5 s for
        # 5 days on 2026-11-02 15:29); the whole 25-day window in one query ran out of query memory on the shared
        # TDengine (0x073a) and took over 160 s. A slice that still runs out of memory is split in half and retried.
        # For an interval amount, subtract the cumulative value of the earlier slice end.
        wanted = set(dates)
        for index in range(0, len(dates), SLICE_DAYS):
            result.extend(_read_0945_slice(cursor, list(dates[index:index + SLICE_DAYS]), wanted))
    finally:
        connection.close()
    return result


def _obs_store():
    from .runtime_state import StateStore, redis_configured
    return StateStore() if redis_configured() else None


def _cached_day(store: Any, day: str) -> list[dict[str, Any]] | None:
    """The kept observations of ``day``, or None when there are none or they do not look like a finished day."""
    if store is None:
        return None
    from .runtime_state import StateUnavailable
    try:
        value = store.get(OBS_KEY_PREFIX + day)
    except StateUnavailable:
        return None
    if not isinstance(value, dict) or set(value) != set(SLOTS):
        return None
    rows: list[dict[str, Any]] = []
    for slot in SLOTS:
        amounts = value[slot]
        if not isinstance(amounts, dict) or len(amounts) < MIN_FINAL_SYMBOLS or not all(
                isinstance(amount, int) and not isinstance(amount, bool) for amount in amounts.values()):
            return None
        rows.extend({"trade_date": day, "slot": slot, "symbol": symbol, "amount_yuan": amount}
                    for symbol, amount in amounts.items())
    return rows


def _keep_day(store: Any, day: str, rows: Sequence[Mapping[str, Any]]) -> bool:
    """Keep ``day`` when both slots were captured whole; a thin day is read again next time, never frozen."""
    if store is None:
        return False
    kept: dict[str, dict[str, int]] = {slot: {} for slot in SLOTS}
    for row in rows:
        if row["trade_date"] == day and row["slot"] in kept:
            kept[row["slot"]][row["symbol"]] = int(row["amount_yuan"])
    if any(len(amounts) < MIN_FINAL_SYMBOLS for amounts in kept.values()):
        return False
    from .runtime_state import StateUnavailable
    try:
        store.set(OBS_KEY_PREFIX + day, kept, OBS_TTL_SECONDS)
    except StateUnavailable:
        return False
    return True


def read_observations(dates: Sequence[str], *, store: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Observations of ``dates``: finished days from the Redis copy, only the missing ones from TD. Returns the rows and
    what was done: {"from_cache": n, "from_td": n, "kept": n}."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for day in dates:
        cached = _cached_day(store, day)
        if cached is None:
            missing.append(day)
        else:
            rows.extend(cached)
    kept = 0
    if missing:
        fresh = _read_days(missing)
        rows.extend(fresh)
        kept = sum(_keep_day(store, day, fresh) for day in missing)
    return rows, {"from_cache": len(dates) - len(missing), "from_td": len(missing), "kept": kept}


def _read_td(target_date: str, dates: Sequence[str]) -> list[dict[str, Any]]:
    return read_observations(list(dates), store=_obs_store())[0] if dates else []


def _membership_inputs(catalog: ArtifactCatalog, dates: Sequence[str]):
    payload: dict[str, list[dict[str, Any]]] = {}
    versions: dict[str, str] = {}
    for day in dates:
        path = catalog.locate("plate_members", day)
        if not path:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            payload[day] = catalog.read_rows(path)
            versions[day] = str(manifest.get("version") or "unknown")
        except (OSError, ValueError, TypeError):
            continue
    return payload, versions


def _existing(catalog: ArtifactCatalog, target: str):
    path = catalog.locate(DATASET, target)
    if not path:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        catalog_versions = catalog.read().get("datasets", {}).get(DATASET, {}).get("dates", {}).get(target, [])
        if manifest.get("schema") == SCHEMA and any(v.get("version") == manifest.get("version") for v in catalog_versions):
            return manifest
    except (OSError, ValueError, TypeError):
        pass
    return None


def _publish(target: str, manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
             current_codes: set[str]) -> bool:
    url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL")
    if not url:
        return False
    import redis
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("plate_code") or "")
        if code in current_codes:
            grouped.setdefault(code, []).append({k: row.get(k) for k in
                ("plate_code", "slot", "median_yuan", "sample_days", "dates_used", "missing_dates")})
    client = redis.Redis.from_url(url, decode_responses=True)
    for code, compact in grouped.items():
        payload = canonical_json({"dataset": DATASET, "trade_date": target,
                                  "version": manifest.get("version"), "rows": compact})
        client.set(f"core:cache:data:{DATASET}:{target}:{code}", payload.decode(), ex=30 * 86400)
    return True


def build_and_store_plate_amount_baseline(target_date: str, *, artifact_root: str | Path,
                                          publish: bool = True) -> dict[str, Any]:
    target = _day(target_date)
    catalog = ArtifactCatalog(artifact_root)
    old = _existing(catalog, target)
    if old:
        current_rows = catalog.read_rows(catalog.locate("plate_members", target)) if catalog.locate("plate_members", target) else []
        current_codes = {str(row.get("plate_code") or row.get("plateid") or "") for row in current_rows}
        return {"status": old.get("business_status", "PARTIAL"), "reused": True,
                "version": old.get("version"), "row_count": old.get("row_count"),
                "redis_published": _publish(target, old, catalog.read_rows(Path(old["manifest_uri"])) if old.get("manifest_uri") else [], current_codes) if publish else False}
    td_days: list[str] = []
    connection = _td_connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT trade_date FROM market_data1.auction_snapshot_v2 "
                       f'WHERE trade_date < "{target.replace("-", "")}" AND auction_tag="0925" '
                       "ORDER BY trade_date DESC LIMIT 64")
        td_days = sorted({_day(row[0]) for row in cursor.fetchall()})
    finally:
        connection.close()
    dates = sorted(set(td_days))[-WINDOW_DAYS:]
    membership_by_date, versions = _membership_inputs(catalog, dates)
    available = datetime.now(timezone.utc).isoformat()
    observations, observation_source = read_observations(dates, store=_obs_store()) if dates else ([], {})
    built = build_plate_amount_baseline(target_date=target, membership_by_date=membership_by_date,
        observations=observations, candidate_dates=dates, membership_versions=versions, available_at=available)
    manifest = {"schema": SCHEMA, "dataset": DATASET, "trade_date": target,
        "point_in_time_date": max(dates) if dates else None, "available_at": available,
        "source": ["market_data1.auction_snapshot_v2", "market_data1.stock_tick_v2", "plate_members"],
        "business_status": built["status"], "window": {"sessions": WINDOW_DAYS,
        "excludes_target_date": True, "dates": dates}, "membership_versions": versions}
    committed = catalog.commit(DATASET, target, built["rows"], manifest, source="core_td_plate_amount")
    current_path = catalog.locate("plate_members", target)
    current_members = catalog.read_rows(current_path) if current_path else []
    current_codes = {str(row.get("plate_code") or row.get("plateid") or "") for row in current_members}
    published = _publish(target, committed, built["rows"], current_codes) if publish else False
    return {"status": built["status"], "reused": False, "version": committed["version"],
            "sha256": committed["sha256"], "row_count": committed["row_count"],
            "available_at": committed["available_at"], "date_count": len(dates),
            "membership_date_count": len(membership_by_date), "observations": observation_source,
            "redis_published": published}
