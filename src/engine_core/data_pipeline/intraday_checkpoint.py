"""Intraday checkpoint snapshot from the live Redis ``q2:{symbol}`` hashes (C587).

Redis keeps only the latest hash per symbol (no history), so a checkpoint can only be captured while the feed's
own per-row ``ts`` (epoch ms) lies in ``[cutoff, cutoff + tolerance]``.  Rows outside that window are *not* this
checkpoint (earlier = stale for the cutoff, later = a newer tick that overwrote it) and are counted, never kept.
A missed window is reported as missed; nothing is back-filled or relabelled.

Pure core (design/24 section 1): ``build_intraday_checkpoint(request) -> Partition``.  No environment, no I/O.
``read_q2`` is the only function that touches Redis and uses read commands only (SMEMBERS, HGETALL).

Units (verified on the live feed, same as ``Q2FinalProvider``): ``px/pc/mx/mn`` milli-yuan (9790 = 9.79 yuan),
``amt`` yuan, ``vol`` hands (1 hand = 100 shares); ``ts`` epoch milliseconds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

DATASET = "intraday_checkpoint"
SOURCE = "redis_q2_checkpoint"
SCHEMA_VERSION = 1
SHANGHAI = timezone(timedelta(hours=8))
EXPECTED_BASIS = "q2:active:{YYYYMMDD} member set read in the same call"
FIELD_UNITS = {"price": "yuan", "pre_close": "yuan", "high": "yuan", "low": "yuan", "pct_chg": "percent",
               "amount_yuan": "yuan", "volume_hands": "hands(100 shares)", "ts": "ISO-8601 +08:00 (row evidence)"}
SOURCE_COLUMNS = ("price", "pre_close", "high", "low", "pct_chg", "amount_yuan", "volume_hands", "phase")
DEFAULTS = {"sources": ["redis_q2", "td"], "tolerance_seconds": 60, "min_symbols": 4000,
            "td_until": "09:45:59", "redis_timeout_seconds": 5.0}


class CheckpointError(Exception):
    def __init__(self, code: str, detail: str, *, exit_code: int, retryable: bool = False):
        super().__init__(f"{code}:{detail}")
        self.code, self.detail, self.exit_code, self.retryable = code, detail, exit_code, retryable


@dataclass
class Partition:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]
    notes: dict[str, Any] = field(default_factory=dict)


def parse_config(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Event ``args.checkpoint`` merged over DEFAULTS; unknown keys rejected so a typo never silently disables a rule."""
    raw = (args or {}).get("checkpoint")
    cfg = dict(DEFAULTS)
    if raw is None:
        return cfg
    if not isinstance(raw, Mapping):
        raise CheckpointError("CONFIG_ERROR", "checkpoint_config_not_object", exit_code=64)
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise CheckpointError("CONFIG_ERROR", "unknown_checkpoint_keys:" + ",".join(unknown), exit_code=64)
    cfg.update(raw)
    sources = cfg["sources"]
    if (not isinstance(sources, (list, tuple)) or not sources
            or any(s not in {"redis_q2", "td"} for s in sources)):
        raise CheckpointError("CONFIG_ERROR", "bad_sources", exit_code=64)
    cfg["sources"] = list(sources)
    cfg["tolerance_seconds"] = float(cfg["tolerance_seconds"])
    cfg["min_symbols"] = int(cfg["min_symbols"])
    cfg["redis_timeout_seconds"] = float(cfg["redis_timeout_seconds"])
    if cfg["tolerance_seconds"] <= 0 or cfg["min_symbols"] <= 0 or cfg["redis_timeout_seconds"] <= 0:
        raise CheckpointError("CONFIG_ERROR", "non_positive_value", exit_code=64)
    return cfg


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def row_time(raw: Mapping[Any, Any]) -> datetime | None:
    """The row's own time evidence: ``ts`` epoch ms -> +08:00 datetime (None when absent/invalid)."""
    values = {_text(k): v for k, v in raw.items()}
    stamp = _num(values.get("ts"))
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp / 1000.0, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def q2_row(symbol: str, raw: Mapping[Any, Any], observed: datetime, trade_date: str) -> dict[str, Any]:
    values = {_text(k): v for k, v in raw.items()}
    px, pc = _num(values.get("px")), _num(values.get("pc"))
    high, low = _num(values.get("mx")), _num(values.get("mn"))
    amount, volume = _num(values.get("amt")), _num(values.get("vol"))
    phase = values.get("ph")
    return {"symbol": symbol, "date": trade_date,
            "ts": observed.isoformat(timespec="milliseconds"),
            "price": None if px is None else round(px / 1000.0, 3),
            "pre_close": None if pc is None else round(pc / 1000.0, 3),
            "high": None if high is None else round(high / 1000.0, 3),
            "low": None if low is None else round(low / 1000.0, 3),
            "pct_chg": None if px is None or not pc else round((px / pc - 1.0) * 100.0, 4),
            "amount_yuan": amount, "volume_hands": volume,
            "phase": None if phase is None else _text(phase)}


def cutoff_datetime(trade_date: str, cutoff: str) -> datetime:
    hh, mm, ss = (int(part) for part in (cutoff.split(":") + ["0", "0"])[:3])
    day = datetime.fromisoformat(trade_date)
    return day.replace(hour=hh, minute=mm, second=ss, tzinfo=SHANGHAI)


def classify(raw_by_symbol: Mapping[str, Mapping[Any, Any]], trade_date: str, cutoff: datetime,
             tolerance_seconds: float) -> tuple[dict[str, tuple[datetime, Mapping[Any, Any]]], dict[str, int]]:
    """Split rows by their own ts: in window / before / after / other date / no ts / empty."""
    upper = cutoff + timedelta(seconds=tolerance_seconds)
    inside: dict[str, tuple[datetime, Mapping[Any, Any]]] = {}
    counts = {"in_window": 0, "before_window": 0, "after_window": 0, "other_date": 0, "no_ts": 0, "empty": 0}
    for symbol, raw in raw_by_symbol.items():
        if not raw:
            counts["empty"] += 1
            continue
        observed = row_time(raw)
        if observed is None:
            counts["no_ts"] += 1
        elif observed.date().isoformat() != trade_date:
            counts["other_date"] += 1
        elif observed < cutoff:
            counts["before_window"] += 1
        elif observed > upper:
            counts["after_window"] += 1
        else:
            counts["in_window"] += 1
            inside[symbol] = (observed, raw)
    return inside, counts


def window_state(counts: Mapping[str, int], expected: int, min_symbols: int) -> str:
    """``ready`` | ``not_yet`` | ``passed`` for the current counts (the gate and the capture share this)."""
    if counts["in_window"] >= min_symbols:
        return "ready"
    # Rows already past the window can never come back; if so many are gone that the threshold is unreachable the
    # checkpoint is lost for good.
    reachable = expected - counts["after_window"]   # earlier / yesterday / empty rows can still tick into the window
    return "passed" if reachable < min_symbols else "not_yet"


def build_intraday_checkpoint(request: Mapping[str, Any]) -> Partition:
    """``request``: trade_date, cutoff (HH:MM:SS), tolerance_seconds, min_symbols, symbols (active set), raw
    ({symbol: q2 hash}), read_at (ISO, when Redis was read), provider_attempts (optional list)."""
    trade_date = str(request["trade_date"])
    cutoff = cutoff_datetime(trade_date, str(request["cutoff"]))
    tolerance = float(request["tolerance_seconds"])
    min_symbols = int(request["min_symbols"])
    symbols = sorted({str(s).zfill(6) for s in request.get("symbols") or ()})
    raw = {str(k).zfill(6): v for k, v in (request.get("raw") or {}).items()}
    universe = {s: raw.get(s) or {} for s in symbols}
    inside, counts = classify(universe, trade_date, cutoff, tolerance)
    expected = len(symbols)
    state = window_state(counts, expected, min_symbols)
    rows = [q2_row(sym, raw_row, observed, trade_date) for sym, (observed, raw_row) in sorted(inside.items())]
    stamps = [row["ts"] for row in rows]
    count = len(rows)
    filled = (sum(1 for row in rows for col in SOURCE_COLUMNS if row.get(col) is not None) /
              (count * len(SOURCE_COLUMNS))) if count else 0.0
    quality = {"status": "READY" if state == "ready" else "FAILED", "coverage": round(count / expected, 6) if expected else None,
               "expected_basis": EXPECTED_BASIS, "expected_count": expected, "missing": max(0, expected - count),
               "field_completeness": round(filled, 6), "partial": count < expected, "row_count": count,
               "min_symbols": min_symbols, "window_state": state, "dropped": dict(counts)}
    manifest = {"schema_version": SCHEMA_VERSION, "dataset": DATASET, "trade_date": trade_date, "source": SOURCE,
                "provider": SOURCE, "provider_chain": list(request.get("provider_chain") or ["redis_q2", "td"]),
                "provider_attempts": list(request.get("provider_attempts") or []),
                "checkpoint": str(request["cutoff"]), "tolerance_seconds": tolerance,
                "window": [cutoff.isoformat(), (cutoff + timedelta(seconds=tolerance)).isoformat()],
                "as_of": cutoff.isoformat(), "captured_at": request.get("read_at"),
                "business_status": "PROVISIONAL", "degraded": False, "row_count": count, "quality": quality,
                "field_units": dict(FIELD_UNITS),
                "source_dates": {"date_evidence": "per-row ts (epoch ms) must be on trade_date and inside the window",
                                 "row_ts_min": min(stamps) if stamps else None,
                                 "row_ts_max": max(stamps) if stamps else None},
                "source_dependency": "legacy_redis_q2 (read-only)"}
    return Partition(rows, manifest, {"counts": counts, "state": state, "expected": expected})


def read_q2(redis: Any, trade_date: str) -> tuple[list[str], dict[str, dict[Any, Any]]]:
    """Read-only: SMEMBERS q2:active:YYYYMMDD then HGETALL q2:{symbol} in one non-transactional pipeline."""
    members = redis.smembers("q2:active:" + trade_date.replace("-", ""))
    symbols = sorted({_text(m).zfill(6) for m in members})
    if not symbols:
        return [], {}
    pipe = redis.pipeline(transaction=False)
    for symbol in symbols:
        pipe.hgetall("q2:" + symbol)
    return symbols, {s: (h or {}) for s, h in zip(symbols, pipe.execute())}


def locate_checkpoint(catalog: Any, trade_date: str, cutoff: str):
    """Manifest path of the newest committed version for one checkpoint time (versions of a day hold all of them)."""
    import json
    from pathlib import Path
    versions = catalog.read().get("datasets", {}).get(DATASET, {}).get("dates", {}).get(trade_date, [])
    for version in reversed(versions):
        path = Path(version["manifest"])
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("checkpoint") == cutoff:
                return path
        except (OSError, ValueError):
            continue
    return None
