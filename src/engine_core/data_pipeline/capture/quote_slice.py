"""Capture raw Redis Q2 and TD auction rows as independent quote-slice versions."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCES = ("redis_q2", "td_auction_snapshot_v2")
TD_COLUMNS = ("ts", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan",
              "rest_ask_amt_yuan", "limit_state", "symbol", "trade_date", "auction_tag")
TIMES = {"0920": ("09:20:25", "09:20:55"),
         "0924": ("09:24:25", "09:24:55"),
         "0925": ("09:25:25", "09:25:55")}


def _text(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if value is None or isinstance(value, str) else str(value)


def _clock(now: datetime | Callable[[], datetime] | None) -> datetime:
    value = now() if callable(now) else now
    if value is None:
        return datetime.now(SHANGHAI)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must produce a timezone-aware datetime")
    return value.astimezone(SHANGHAI)


def _event_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)
    text = str(value).strip()
    try:
        number = float(text)
        if number >= 100_000_000_000:
            return datetime.fromtimestamp(number / 1000, timezone.utc).astimezone(SHANGHAI)
        if number >= 1_000_000_000:
            return datetime.fromtimestamp(number, timezone.utc).astimezone(SHANGHAI)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def _max_event(rows: Sequence[Mapping[str, Any]]) -> str | None:
    values = [parsed for row in rows if (parsed := _event_time(row.get("ts"))) is not None]
    return max(values).isoformat(timespec="milliseconds") if values else None


def _row_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return str(row.get("symbol", "")), str(row.get("ts", "")), encoded


def _redis_rows(client: Any, trade_date: str, tag: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    compact = trade_date.replace("-", "")
    symbols = sorted({_text(item) for item in client.smembers(f"q2:active:{compact}")})
    pipe = client.pipeline(transaction=False)
    for symbol in symbols:
        pipe.hgetall(f"q2:{symbol}")
    aux_keys = (f"a2:{compact}:{tag}", f"market:auction:{compact}:{tag}")
    for key in aux_keys:
        pipe.hget(key, "meta")
    values = pipe.execute()
    raw_hashes, aux_values = values[:len(symbols)], values[len(symbols):]
    missing = []
    rows = []
    for symbol, raw in zip(symbols, raw_hashes):
        if not raw:
            missing.append(symbol)
            continue
        row = {str(_text(key)): _text(value) for key, value in raw.items()}
        rows.append({"symbol": symbol, "trade_date": trade_date, "tag": tag, **row})
    aux = {key: _text(value) for key, value in zip(aux_keys, aux_values) if value is not None}
    return rows, len(symbols), {"missing_hashes": {"count": len(missing), "symbols": missing[:20]},
                                "redis_aux": aux}


def _td_rows(client: Any, trade_date: str, tag: str) -> tuple[list[dict[str, Any]], int]:
    compact = trade_date.replace("-", "")
    sql = ("select ts,px_milli,chg_bp,match_amt_yuan,rest_bid_amt_yuan,rest_ask_amt_yuan,"
           "limit_state,symbol,trade_date,auction_tag from market_data1.auction_snapshot_v2 "
           f"where trade_date='{compact}' and auction_tag='{tag}'")
    cursor = client.cursor() if callable(getattr(client, "cursor", None)) else client
    try:
        cursor.execute(sql)
        fetched = cursor.fetchall() or []
        rows = []
        for item in fetched:
            row = dict(item) if isinstance(item, Mapping) else dict(zip(TD_COLUMNS, item))
            if row.get("ts") is not None:
                row["ts"] = str(row["ts"])
            rows.append(row)
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    rows.sort(key=_row_order)
    return rows, len(rows)


def capture_quote_slice(trade_date: str, tag: str, *, redis_client: Any, td_client: Any,
                        catalog: Any, now: datetime | Callable[[], datetime] | None = None,
                        sources: Sequence[str] = SOURCES) -> dict[str, Any]:
    """Read and commit each requested source separately; failures stay source-local."""
    try:
        parsed_date = date.fromisoformat(trade_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must be YYYY-MM-DD") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must be YYYY-MM-DD")
    if tag not in TIMES:
        raise ValueError("tag must be one of 0920, 0924, 0925")
    if len(set(sources)) != len(sources) or any(source not in SOURCES for source in sources):
        raise ValueError("sources contains an unsupported or duplicate source")

    target, cutoff = TIMES[tag]
    target_time = datetime.fromisoformat(f"{trade_date}T{target}+08:00")
    window_end = datetime.fromisoformat(f"{trade_date}T{cutoff}+08:00")
    clients = {"redis_q2": redis_client, "td_auction_snapshot_v2": td_client}
    committed: dict[str, Any] = {}
    failed: dict[str, Any] = {}
    timings: dict[str, float] = {}
    for source in sources:
        started = time.perf_counter()
        try:
            read_start = _clock(now)
            if source == "redis_q2":
                rows, cohort_size, extra = _redis_rows(clients[source], trade_date, tag)
                coverage_basis = "q2:active:<YYYYMMDD> member count"
            else:
                rows, cohort_size = _td_rows(clients[source], trade_date, tag)
                extra = {}
                coverage_basis = "same-date and tag SELECT row count; external universe not independently known"
            read_end = _clock(now)
            rows.sort(key=_row_order)
            coverage = len(rows) / cohort_size if cohort_size else None
            manifest = {
                "source": source, "adapter": source, "tag": tag,
                "read_start": read_start.isoformat(timespec="microseconds"),
                "read_end": read_end.isoformat(timespec="microseconds"),
                "captured_at": read_end.isoformat(timespec="microseconds"),
                "target_time": target_time.isoformat(timespec="seconds"),
                "window_end": window_end.isoformat(timespec="seconds"),
                "business_status": "FORMAL" if read_end <= window_end else "LATE_CAPTURE",
                "cohort_size": cohort_size, "source_event_max_ts": _max_event(rows),
                "quality": {"coverage": coverage, "coverage_basis": coverage_basis},
                **extra,
            }
            committed[source] = catalog.commit(
                "quote_slice", trade_date, rows, manifest, source=source, adapter=source,
                context={"trade_date": trade_date, "tag": tag})
        except Exception as exc:
            failed[source] = {"error_type": type(exc).__name__, "message": str(exc)}
        timings[source] = time.perf_counter() - started
    return {"committed": committed, "failed": failed, "timings_seconds": timings}
