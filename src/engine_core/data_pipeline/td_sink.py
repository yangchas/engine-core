"""Core-owned TDengine sink for the ``daily_kline`` dataset (C567, docs/DAILY_KLINE_TD.md).

The catalog stays the transitional, versioned ledger. This module copies the highest-quality catalog version of a
trading day into the Core-owned TD database (never ``market_data1``), reads it back and verifies it, and exposes the
per-day version metadata that the guardian self-check uses to decide whether a day is missing or still low quality.

Storage layer (``TdStore``) and sync logic (``DailyKlineTdSink``) are separate so the logic is unit-tested against an
in-memory fake and the SQL against a real temporary database.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .derived_daily import STATUS_RANK, normalize_status

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = "core_data"
SUPER_TABLE = "daily_kline_v1"
META_TABLE = "daily_kline_versions"
SCHEMA_VERSION = 1
FORBIDDEN_DATABASES = frozenset({"market_data1", "information_schema", "performance_schema", "log"})
_DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SYMBOL = re.compile(r"^[0-9A-Za-z]{1,8}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSERT_CHUNK_ROWS = 400
SUM_REL_TOLERANCE = 1e-9
SAMPLE_ROWS = 120


class TdUnavailable(RuntimeError):
    """TD cannot be reached or a statement failed transiently: retryable, never read as "data missing"."""


class TdVerifyError(RuntimeError):
    """Read-back differs from the catalog: deterministic, the day is not complete."""


class TdSourceRejected(ValueError):
    """Only a partition whose actual provider is Baostock may enter daily_kline_v1."""


def validate_database_name(name: str) -> str:
    text = str(name or "")
    if not _DB_NAME.match(text) or text in FORBIDDEN_DATABASES:
        raise ValueError("invalid_or_forbidden_td_database:" + text)
    return text


# ---------------------------------------------------------------------------------------------------------------------
# Column mapping (catalog row key -> TD column). The ONLY place where units are converted.
# ---------------------------------------------------------------------------------------------------------------------
LOTS_PER_SHARE_FACTOR = 100  # 1 lot (手) = 100 shares


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def volume_factor(manifest: Mapping[str, Any]) -> int:
    """Shares per catalog volume unit. Unknown units are never guessed except where C579 proved lots (q2_final)."""
    units = manifest.get("field_units")
    unit = str(units.get("volume") if isinstance(units, Mapping) else "").lower()
    if unit in {"shares", "share", "股"}:
        return 1
    if unit in {"lots", "lot", "hand", "手"}:
        return LOTS_PER_SHARE_FACTOR
    provider = str(manifest.get("provider") or manifest.get("source") or "")
    if provider in {"q2_final", "legacy_q2_final"}:
        return LOTS_PER_SHARE_FACTOR  # C579: legacy q2_final partitions carry lots with an UNKNOWN unit label
    return 1


def _volume_shares(row: Mapping[str, Any], factor: int) -> int | None:
    value = _num(row.get("volume"))
    return None if value is None else int(round(value * factor))


# (td_column, td_type, extractor(row, factor)); order = insert order
COLUMN_MAP: tuple[tuple[str, str, Callable[[Mapping[str, Any], int], Any]], ...] = (
    ("open", "DOUBLE", lambda r, f: _num(r.get("open"))),
    ("high", "DOUBLE", lambda r, f: _num(r.get("high"))),
    ("low", "DOUBLE", lambda r, f: _num(r.get("low"))),
    ("close", "DOUBLE", lambda r, f: _num(r.get("close"))),
    ("pre_close", "DOUBLE", lambda r, f: _num(r.get("pre_close"))),
    ("pct_chg", "DOUBLE", lambda r, f: _num(r.get("pct_chg"))),
    ("volume_shares", "BIGINT", lambda r, f: _volume_shares(r, f)),
    ("amount_yuan", "DOUBLE", lambda r, f: _num(_first(r, "amount_yuan", "amount"))),
    ("turnover_pct", "DOUBLE", lambda r, f: _num(_first(r, "turnover_pct", "turnover"))),
    ("suspended", "BOOL", lambda r, f: r["suspended"] if isinstance(r.get("suspended"), bool) else None),
)
DATA_COLUMNS = tuple(name for name, _t, _f in COLUMN_MAP)


def row_symbol(row: Mapping[str, Any]) -> str | None:
    symbol = str(row.get("symbol") or "").strip()
    if not symbol and row.get("code"):
        symbol = str(row["code"]).rsplit(".", 1)[-1]
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    return symbol if _SYMBOL.match(symbol) else None


def map_rows(rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], int]:
    """Catalog rows -> {symbol: {td column: value}}; the last row of a repeated symbol wins. Returns (mapped, dropped)."""
    factor = volume_factor(manifest)
    mapped: dict[str, dict[str, Any]] = {}
    dropped = 0
    for row in rows:
        symbol = row_symbol(row)
        if symbol is None:
            dropped += 1
            continue
        if symbol in mapped:
            dropped += 1
        mapped[symbol] = {name: extract(row, factor) for name, _type, extract in COLUMN_MAP}
    return mapped, dropped


def expected_aggregates(rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Independent catalog-side aggregates used for read-back verification (does not use COLUMN_MAP)."""
    factor = volume_factor(manifest)
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        symbol = row_symbol(row)
        if symbol is not None:
            latest[symbol] = row
    out = {"count": len(latest), "sum_close": 0.0, "n_close": 0, "sum_amount": 0.0, "n_amount": 0,
           "sum_volume": 0, "n_volume": 0, "sum_pct": 0.0, "n_pct": 0, "n_open": 0, "n_turnover": 0}
    for row in latest.values():
        close = _num(row.get("close"))
        if close is not None:
            out["sum_close"] += close
            out["n_close"] += 1
        amount = _num(_first(row, "amount_yuan", "amount"))
        if amount is not None:
            out["sum_amount"] += amount
            out["n_amount"] += 1
        volume = _num(row.get("volume"))
        if volume is not None:
            out["sum_volume"] += int(round(volume * factor))
            out["n_volume"] += 1
        pct = _num(row.get("pct_chg"))
        if pct is not None:
            out["sum_pct"] += pct
            out["n_pct"] += 1
        if _num(row.get("open")) is not None:
            out["n_open"] += 1
        if _num(_first(row, "turnover_pct", "turnover")) is not None:
            out["n_turnover"] += 1
    return out


def sample_symbols(symbols: Sequence[str], size: int = SAMPLE_ROWS) -> list[str]:
    ordered = sorted(symbols)
    if len(ordered) <= size:
        return ordered
    step = len(ordered) / size
    return [ordered[int(i * step)] for i in range(size)]


# ---------------------------------------------------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------------------------------------------------
def trade_day_ts_us(trade_date: str) -> int:
    day = datetime.fromisoformat(trade_date).replace(tzinfo=SHANGHAI)
    return int(day.timestamp()) * 1_000_000


def _parse_instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)


def version_rank(business_status: Any, needs_upgrade: bool) -> tuple[int, int]:
    """FORMAL > PROVISIONAL > DEGRADED; within a level a version that needs no upgrade wins (legacy unlabeled READY)."""
    return STATUS_RANK[normalize_status(business_status)], 0 if needs_upgrade else 1


def pick_best(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Highest rank; ties go to the latest ``available_at``, then to the later list position (newest commit)."""
    best, best_key = None, None
    for index, item in enumerate(candidates):
        moment = _parse_instant(item.get("available_at") or item.get("committed_at"))
        key = (*version_rank(item.get("business_status"), bool(item.get("needs_upgrade"))),
               moment.timestamp() if moment else 0.0, index)
        if best_key is None or key >= best_key:
            best, best_key = item, key
    return best


# ---------------------------------------------------------------------------------------------------------------------
# Store interface and implementations
# ---------------------------------------------------------------------------------------------------------------------
class TdStore(Protocol):
    def schema_exists(self) -> bool: ...
    def ensure_schema(self) -> None: ...
    def read_versions(self, trade_date: str) -> list[dict[str, Any]]: ...
    def write_version(self, meta: Mapping[str, Any]) -> None: ...
    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None: ...
    def day_aggregates(self, trade_date: str) -> dict[str, Any]: ...
    def read_day_rows(self, trade_date: str, symbols: Sequence[str]) -> dict[str, dict[str, Any]]: ...
    def day_counts(self, dates: Sequence[str]) -> dict[str, int]: ...
    def close(self) -> None: ...


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def ddl_statements(database: str) -> list[str]:
    db = validate_database_name(database)
    columns = ", ".join(f"{name} {type_}" for name, type_, _f in COLUMN_MAP)
    return [
        f"CREATE DATABASE IF NOT EXISTS {db} PRECISION 'us' KEEP 3650d DURATION 90d VGROUPS 2",
        f"CREATE STABLE IF NOT EXISTS {db}.{SUPER_TABLE} (ts TIMESTAMP, {columns}, version VARCHAR(24)) "
        "TAGS (symbol VARCHAR(8))",
        f"CREATE TABLE IF NOT EXISTS {db}.{META_TABLE} (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), "
        "business_status VARCHAR(16), rank_level INT, needs_upgrade BOOL, source VARCHAR(96), provider VARCHAR(32), "
        "degraded BOOL, quality VARCHAR(4000), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), "
        "synced_at VARCHAR(40), schema_version INT)",
    ]


class TaosStore:
    """Real TDengine store (taos native client). Only ever touches ``<database>.*`` created by this module."""

    def __init__(self, database: str, *, connect: Callable[[], Any] | None = None):
        self.database = validate_database_name(database)
        self._connect_fn = connect or self._default_connect
        self._conn: Any = None

    @staticmethod
    def _default_connect() -> Any:
        if not os.environ.get("TD_CONFIG"):
            raise TdUnavailable("TD_CONFIG_required")
        import taos
        options: dict[str, Any] = {"config": os.environ["TD_CONFIG"]}
        for env_name, option in (("TDENGINE_HOST", "host"), ("TDENGINE_PORT", "port"),
                                 ("TDENGINE_USER", "user"), ("TDENGINE_PASSWORD", "password")):
            if os.environ.get(env_name):
                options[option] = int(os.environ[env_name]) if option == "port" else os.environ[env_name]
        return taos.connect(**options)

    def _cursor(self):
        if self._conn is None:
            try:
                self._conn = self._connect_fn()
            except TdUnavailable:
                raise
            except Exception as exc:
                raise TdUnavailable("td_connect_failed:" + type(exc).__name__ + ":" + str(exc)[:160]) from exc
        return self._conn.cursor()

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        errno = (getattr(exc, "errno", 0) or 0) & 0xFFFF
        return errno in {0x0388, 0x2603, 0x2662} or "not exist" in str(exc).lower()

    def _run(self, sql: str, *, missing_ok: bool = False) -> list[tuple]:
        cursor = self._cursor()
        try:
            cursor.execute(sql)
            try:
                return list(cursor.fetchall())
            except Exception:
                return []
        except Exception as exc:
            if missing_ok and self._is_missing(exc):
                return []
            self.close()
            raise TdUnavailable("td_statement_failed:" + type(exc).__name__ + ":" + str(exc)[:200]) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def schema_exists(self) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute("SHOW DATABASES")
            names = {row[0] for row in cursor.fetchall()}
        except Exception as exc:
            self.close()
            raise TdUnavailable("td_statement_failed:" + str(exc)[:200]) from exc
        if self.database not in names:
            return False
        rows = self._run(f"SELECT COUNT(*) FROM {self.database}.{META_TABLE}", missing_ok=True)
        return bool(rows) and self._run(f"DESCRIBE {self.database}.{SUPER_TABLE}", missing_ok=True) != []

    def ensure_schema(self) -> None:
        for statement in ddl_statements(self.database):
            self._run(statement)

    # -- versions ---------------------------------------------------------------------------------------------------
    _META_COLUMNS = ("ts", "trade_date", "version", "business_status", "rank_level", "needs_upgrade", "source",
                     "provider", "degraded", "quality", "row_count", "sha256", "available_at", "synced_at",
                     "schema_version")

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        _check_date(trade_date)
        rows = self._run(f"SELECT CAST(ts AS BIGINT), {', '.join(self._META_COLUMNS[1:])} "
                         f"FROM {self.database}.{META_TABLE} WHERE trade_date = '{trade_date}' ORDER BY ts",
                         missing_ok=True)
        out = []
        for row in rows:
            item = dict(zip(self._META_COLUMNS, row))
            try:
                item["quality"] = json.loads(item["quality"]) if item.get("quality") else {}
            except ValueError:
                item["quality"] = {}
            out.append(item)
        return out

    def write_version(self, meta: Mapping[str, Any]) -> None:
        values = dict(meta)
        values["quality"] = json.dumps(values.get("quality") or {}, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
        if len(values["quality"].encode("utf-8")) > 3800:
            values["quality"] = json.dumps({"truncated": True})
        statement = (f"INSERT INTO {self.database}.{META_TABLE} ({', '.join(self._META_COLUMNS)}) VALUES ("
                     + ", ".join(_literal(values.get(column)) for column in self._META_COLUMNS) + ")")
        self._run(statement)

    # -- day rows ---------------------------------------------------------------------------------------------------
    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        _check_date(trade_date)
        ts = trade_day_ts_us(trade_date)
        self._run(f"DELETE FROM {self.database}.{SUPER_TABLE} WHERE ts = {ts}", missing_ok=True)
        items = sorted(mapped.items())
        for start in range(0, len(items), INSERT_CHUNK_ROWS):
            parts = []
            for symbol, values in items[start:start + INSERT_CHUNK_ROWS]:
                if not _SYMBOL.match(symbol):
                    raise ValueError("bad_symbol:" + symbol)
                literal = ", ".join(_literal(values.get(name)) for name in DATA_COLUMNS)
                parts.append(f"{self.database}.d_{symbol} USING {self.database}.{SUPER_TABLE} TAGS('{symbol}') "
                             f"VALUES ({ts}, {literal}, {_literal(version)})")
            self._run("INSERT INTO " + " ".join(parts))

    def _day_filter(self, trade_date: str) -> str:
        return f"ts = {trade_day_ts_us(trade_date)}"

    def day_aggregates(self, trade_date: str) -> dict[str, Any]:
        _check_date(trade_date)
        rows = self._run(
            "SELECT COUNT(*), SUM(close), COUNT(close), SUM(amount_yuan), COUNT(amount_yuan), SUM(volume_shares), "
            "COUNT(volume_shares), SUM(pct_chg), COUNT(pct_chg), COUNT(open), COUNT(turnover_pct) "
            f"FROM {self.database}.{SUPER_TABLE} WHERE {self._day_filter(trade_date)}", missing_ok=True)
        if not rows:
            return {"count": 0, "sum_close": 0.0, "n_close": 0, "sum_amount": 0.0, "n_amount": 0, "sum_volume": 0,
                    "n_volume": 0, "sum_pct": 0.0, "n_pct": 0, "n_open": 0, "n_turnover": 0}
        r = rows[0]
        return {"count": int(r[0] or 0), "sum_close": float(r[1] or 0.0), "n_close": int(r[2] or 0),
                "sum_amount": float(r[3] or 0.0), "n_amount": int(r[4] or 0), "sum_volume": int(r[5] or 0),
                "n_volume": int(r[6] or 0), "sum_pct": float(r[7] or 0.0), "n_pct": int(r[8] or 0),
                "n_open": int(r[9] or 0), "n_turnover": int(r[10] or 0)}

    def read_day_rows(self, trade_date: str, symbols: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        _check_date(trade_date)
        if symbols is not None and not symbols:
            return {}
        symbol_filter = ""
        if symbols is not None:
            quoted = ",".join("'" + s + "'" for s in symbols if _SYMBOL.match(s))
            if not quoted:
                return {}
            symbol_filter = f" AND symbol IN ({quoted})"
        rows = self._run(f"SELECT symbol, {', '.join(DATA_COLUMNS)}, version FROM {self.database}.{SUPER_TABLE} "
                         f"WHERE {self._day_filter(trade_date)}{symbol_filter}", missing_ok=True)
        return {row[0]: {**dict(zip(DATA_COLUMNS, row[1:1 + len(DATA_COLUMNS)])), "version": row[-1]} for row in rows}

    def day_counts(self, dates: Sequence[str]) -> dict[str, int]:
        out = {}
        for day in dates:
            _check_date(day)
            rows = self._run(f"SELECT COUNT(*) FROM {self.database}.{SUPER_TABLE} WHERE {self._day_filter(day)}",
                             missing_ok=True)
            out[day] = int(rows[0][0]) if rows else 0
        return out

    def latest_watermarks(self) -> dict[str, str]:
        """Fetch every symbol's latest daily-bar date in one stable-level aggregation."""
        rows = self._run(f"SELECT symbol, LAST(ts) FROM {self.database}.{SUPER_TABLE} GROUP BY symbol",
                         missing_ok=True)
        result: dict[str, str] = {}
        for symbol, value in rows:
            if isinstance(value, datetime):
                day = value.date().isoformat()
            elif isinstance(value, _date):
                day = value.isoformat()
            elif isinstance(value, (int, float)):
                day = datetime.fromtimestamp(float(value) / 1_000_000, timezone.utc).astimezone(SHANGHAI).date().isoformat()
            else:
                day = str(value or "")[:10]
            if day:
                result[str(symbol)] = day
        return result

    def drop_database(self) -> None:
        """Only for temporary test databases (core_dev_*): refuses every other name."""
        if not self.database.startswith("core_dev_"):
            raise ValueError("refusing_to_drop_non_temporary_database:" + self.database)
        self._run(f"DROP DATABASE IF EXISTS {self.database}")


def _check_date(value: str) -> None:
    if not _DATE.match(str(value)):
        raise ValueError("bad_trade_date:" + str(value))


class MemoryTdStore:
    """In-memory fake with the same semantics (used by unit tests; can be told to fail)."""

    def __init__(self) -> None:
        self.created = False
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.days: dict[str, dict[str, dict[str, Any]]] = {}
        self.fail: str | None = None  # None | "unavailable"
        self.calls: list[str] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == "unavailable":
            raise TdUnavailable("fake_td_down")

    def schema_exists(self) -> bool:
        self._guard("schema_exists")
        return self.created

    def ensure_schema(self) -> None:
        self._guard("ensure_schema")
        self.created = True

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        self._guard("read_versions")
        return [dict(item) for item in self.versions.get(trade_date, [])]

    def write_version(self, meta: Mapping[str, Any]) -> None:
        self._guard("write_version")
        bucket = self.versions.setdefault(str(meta["trade_date"]), [])
        bucket[:] = [item for item in bucket if item["ts"] != meta["ts"]]
        bucket.append(dict(meta))
        bucket.sort(key=lambda item: item["ts"])

    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        self._guard("replace_day")
        self.days[trade_date] = {symbol: {**dict(values), "version": version} for symbol, values in mapped.items()}

    def day_aggregates(self, trade_date: str) -> dict[str, Any]:
        self._guard("day_aggregates")
        rows = list(self.days.get(trade_date, {}).values())
        agg = {"count": len(rows), "sum_close": 0.0, "n_close": 0, "sum_amount": 0.0, "n_amount": 0, "sum_volume": 0,
               "n_volume": 0, "sum_pct": 0.0, "n_pct": 0, "n_open": 0, "n_turnover": 0}
        for row in rows:
            for column, s, n in (("close", "sum_close", "n_close"), ("amount_yuan", "sum_amount", "n_amount"),
                                 ("volume_shares", "sum_volume", "n_volume"), ("pct_chg", "sum_pct", "n_pct")):
                if row.get(column) is not None:
                    agg[s] += row[column]
                    agg[n] += 1
            agg["n_open"] += row.get("open") is not None
            agg["n_turnover"] += row.get("turnover_pct") is not None
        return agg

    def read_day_rows(self, trade_date: str, symbols: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        self._guard("read_day_rows")
        day = self.days.get(trade_date, {})
        return ({symbol: dict(values) for symbol, values in day.items()} if symbols is None else
                {symbol: dict(day[symbol]) for symbol in symbols if symbol in day})

    def day_counts(self, dates: Sequence[str]) -> dict[str, int]:
        self._guard("day_counts")
        return {day: len(self.days.get(day, {})) for day in dates}

    def latest_watermarks(self) -> dict[str, str]:
        self._guard("latest_watermarks")
        result: dict[str, str] = {}
        for day, rows in self.days.items():
            for symbol in rows:
                if day > result.get(symbol, ""):
                    result[symbol] = day
        return result

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class SyncOutcome:
    trade_date: str
    status: str            # synced | unchanged | skipped_lower | no_catalog_version | failed
    version: str | None = None
    business_status: str | None = None
    row_count: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date, "status": self.status, "version": self.version,
                "business_status": self.business_status, "row_count": self.row_count, **self.detail}


def catalog_candidates(root: Path, catalog: Mapping[str, Any], trade_date: str) -> list[dict[str, Any]]:
    """Catalog versions of one day with their manifest merged, sample partitions and empty versions removed."""
    from ..guardian.registry import daily_kline_needs_upgrade
    versions = catalog.get("datasets", {}).get("daily_kline", {}).get("dates", {}).get(trade_date) or []
    if isinstance(versions, Mapping):
        versions = [versions]
    out = []
    for position, entry in enumerate(versions):
        if not isinstance(entry, Mapping) or not entry.get("manifest"):
            continue
        try:
            manifest = json.loads(Path(str(entry["manifest"])).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, Mapping) or manifest.get("sample_scope") == "symbols_file":
            continue
        merged = {**manifest, **entry}
        if int(merged.get("row_count") or 0) <= 0 or not merged.get("version"):
            continue
        merged["needs_upgrade"] = daily_kline_needs_upgrade(merged)
        merged["_position"] = position
        out.append(merged)
    return out


class DailyKlineTdSink:
    def __init__(self, store: TdStore, root: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.root = Path(root).expanduser()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _catalog(self) -> dict[str, Any]:
        from .storage import ArtifactCatalog
        return ArtifactCatalog(self.root).read()

    def catalog_dates(self) -> list[str]:
        return sorted(self._catalog().get("datasets", {}).get("daily_kline", {}).get("dates", {}))

    def best_catalog_version(self, trade_date: str, catalog: Mapping[str, Any] | None = None):
        return pick_best(catalog_candidates(self.root, catalog or self._catalog(), trade_date))

    def _meta_for(self, trade_date: str, best: Mapping[str, Any], dropped: int, ts: int) -> dict[str, Any]:
        quality = best.get("quality") if isinstance(best.get("quality"), Mapping) else {}
        keep = {key: quality.get(key) for key in ("status", "coverage", "expected_count", "expected_basis", "partial",
                                                  "missing") if key in quality}
        keep["dropped_rows"] = dropped
        keep["volume_factor"] = volume_factor(best)
        provider = str(best.get("provider") or best.get("source") or "")
        return {"ts": ts, "trade_date": trade_date, "version": str(best["version"]),
                "business_status": normalize_status(best.get("business_status")),
                "rank_level": version_rank(best.get("business_status"), bool(best["needs_upgrade"]))[0],
                "needs_upgrade": bool(best["needs_upgrade"]), "source": str(best.get("source") or "")[:96],
                "provider": provider[:32], "degraded": bool(best.get("degraded")), "quality": keep,
                "row_count": int(best.get("row_count") or 0), "sha256": str(best.get("sha256") or ""),
                "available_at": str(best.get("available_at") or ""),
                "synced_at": self.clock().isoformat(), "schema_version": SCHEMA_VERSION}

    def _meta_ts(self, trade_date: str, best: Mapping[str, Any], existing: Sequence[Mapping[str, Any]]) -> int:
        for item in existing:
            if item.get("version") == best["version"]:
                return int(item["ts"])  # same version again: overwrite the same metadata row
        moment = _parse_instant(best.get("available_at") or best.get("committed_at"))
        ts = int(moment.timestamp() * 1_000_000) if moment else trade_day_ts_us(trade_date)
        taken = {int(item["ts"]) for item in existing}
        while ts in taken:
            ts += 1
        return ts

    def verify_day(self, trade_date: str, best: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                   mapped: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Read back and compare with the catalog: counts, non-null counts, sums and a deterministic row sample."""
        expected = expected_aggregates(rows, best)
        actual = self.store.day_aggregates(trade_date)
        problems: list[str] = []
        for key in ("count", "n_close", "n_amount", "n_volume", "n_pct", "n_open", "n_turnover", "sum_volume"):
            if int(expected[key]) != int(actual[key]):
                problems.append(f"{key}: catalog={expected[key]} td={actual[key]}")
        for key in ("sum_close", "sum_amount", "sum_pct"):
            tolerance = SUM_REL_TOLERANCE * max(1.0, abs(expected[key]))
            if abs(expected[key] - actual[key]) > tolerance:
                problems.append(f"{key}: catalog={expected[key]!r} td={actual[key]!r} tol={tolerance:.3g}")
        sample = sample_symbols(list(mapped), SAMPLE_ROWS)
        read_back = self.store.read_day_rows(trade_date, sample)
        mismatched = 0
        for symbol in sample:
            got = read_back.get(symbol)
            if got is None or got.get("version") != best["version"]:
                mismatched += 1
                continue
            for column in DATA_COLUMNS:
                want, have = mapped[symbol].get(column), got.get(column)
                if want is None or have is None:
                    same = want is None and have is None
                elif isinstance(want, float):
                    same = abs(want - float(have)) <= 1e-9 * max(1.0, abs(want))
                else:
                    same = want == have
                if not same:
                    mismatched += 1
                    problems.append(f"sample {symbol}.{column}: catalog={want!r} td={have!r}")
                    break
        if mismatched and not any(p.startswith("sample") for p in problems):
            problems.append(f"sample rows missing or from another version: {mismatched}")
        report = {"expected": expected, "actual": actual, "sample_size": len(sample), "sample_mismatched": mismatched,
                  "problems": problems}
        if problems:
            raise TdVerifyError("td_readback_mismatch:" + trade_date + ":" + "; ".join(problems[:4]))
        return report

    def write_dataset(self, spec: Any, trade_date: str, rows: Sequence[Mapping[str, Any]],
                      manifest: Mapping[str, Any]) -> SyncOutcome:
        """Persist the just-built Baostock partition and verify its TD version row before returning."""
        if getattr(spec, "name", None) != "daily_kline":
            raise TdSourceRejected("td_daily_kline_sink_rejects_dataset:" + str(getattr(spec, "name", None)))
        return self.write_partition(trade_date, rows, manifest)

    def write_partition(self, trade_date: str, rows: Sequence[Mapping[str, Any]],
                        manifest: Mapping[str, Any]) -> SyncOutcome:
        _check_date(trade_date)
        provider = str(manifest.get("provider") or "").lower()
        if provider != "baostock":
            raise TdSourceRejected("td_daily_kline_requires_provider_baostock:" + (provider or "UNKNOWN"))
        if str(manifest.get("trade_date") or trade_date) != trade_date:
            raise TdVerifyError("td_trade_date_mismatch:" + trade_date)
        version = str(manifest.get("version") or "")
        if not version or not rows or int(manifest.get("row_count") or -1) != len(rows):
            raise TdVerifyError("td_partition_manifest_mismatch:" + trade_date)
        # The artifact digest binds this exact row sequence. The version row is read back after writing and
        # must carry the same digest and row count before the task can report success.
        from .storage import canonical_json
        import hashlib
        digest = hashlib.sha256(b"\n".join(canonical_json(dict(row)) for row in rows) + b"\n").hexdigest()
        if digest != str(manifest.get("sha256") or ""):
            raise TdVerifyError("td_partition_sha256_mismatch:" + trade_date)
        best = dict(manifest)
        from ..guardian.registry import daily_kline_needs_upgrade
        best["needs_upgrade"] = daily_kline_needs_upgrade(best)
        if not self.store.schema_exists():
            self.store.ensure_schema()
        existing = self.store.read_versions(trade_date)
        mapped, dropped = map_rows(rows, best)
        if not mapped:
            raise TdVerifyError("td_partition_maps_to_zero_rows:" + trade_date)
        self.store.replace_day(trade_date, version, mapped)
        report = self.verify_day(trade_date, best, rows, mapped)
        meta = self._meta_for(trade_date, best, dropped, self._meta_ts(trade_date, best, existing))
        self.store.write_version(meta)
        versions = self.store.read_versions(trade_date)
        read_back = next((item for item in versions if str(item.get("version")) == version), None)
        counts = self.store.day_counts([trade_date])
        if (read_back is None or str(read_back.get("sha256") or "") != digest
                or int(read_back.get("row_count") or -1) != len(mapped)
                or counts.get(trade_date) != len(mapped)):
            raise TdVerifyError("td_version_readback_mismatch:" + trade_date)
        return SyncOutcome(trade_date, "synced", version, meta["business_status"], len(mapped),
                           {"sha256": digest, "sample_size": report["sample_size"],
                            "version_row_verified": True})

    def sync_date(self, trade_date: str, catalog: Mapping[str, Any] | None = None) -> SyncOutcome:
        catalog = catalog if catalog is not None else self._catalog()
        best = self.best_catalog_version(trade_date, catalog)
        if best is None:
            return SyncOutcome(trade_date, "no_catalog_version")
        if str(best.get("provider") or "").lower() != "baostock":
            raise TdSourceRejected("td_daily_kline_requires_provider_baostock:" +
                                   str(best.get("provider") or best.get("source") or "UNKNOWN"))
        if not self.store.schema_exists():
            self.store.ensure_schema()
        existing = self.store.read_versions(trade_date)
        current = pick_best(existing)
        if current is not None:
            current_key = version_rank(current.get("business_status"), bool(current.get("needs_upgrade")))
            best_key = version_rank(best.get("business_status"), bool(best["needs_upgrade"]))
            if current_key > best_key:
                return SyncOutcome(trade_date, "skipped_lower", str(current["version"]), current.get("business_status"),
                                   int(current.get("row_count") or 0),
                                   {"catalog_version": best["version"], "reason": "td_has_higher_quality_version"})
            if current["version"] == best["version"]:
                counts = self.store.day_counts([trade_date])
                if counts.get(trade_date) == int(current.get("row_count") or -1):
                    return SyncOutcome(trade_date, "unchanged", str(best["version"]), current.get("business_status"),
                                       int(current["row_count"]))
            elif current_key == best_key:
                incoming, held = (_parse_instant(best.get("available_at") or best.get("committed_at")),
                                  _parse_instant(current.get("available_at")))
                if incoming and held and incoming <= held:
                    counts = self.store.day_counts([trade_date])
                    if counts.get(trade_date) == int(current.get("row_count") or -1):
                        return SyncOutcome(trade_date, "unchanged", str(current["version"]),
                                           current.get("business_status"), int(current["row_count"]),
                                           {"reason": "td_has_equal_newer_version"})
        from .storage import ArtifactCatalog
        rows = ArtifactCatalog(self.root).read_rows(Path(str(best["manifest"])))
        mapped, dropped = map_rows(rows, best)
        self.store.replace_day(trade_date, str(best["version"]), mapped)
        report = self.verify_day(trade_date, best, rows, mapped)
        meta = self._meta_for(trade_date, best, dropped, self._meta_ts(trade_date, best, existing))
        self.store.write_version(meta)
        return SyncOutcome(trade_date, "synced", str(best["version"]), meta["business_status"], len(mapped),
                           {"previous_version": current["version"] if current else None, "dropped_rows": dropped,
                            "sample_size": report["sample_size"]})

    def sync_dates(self, dates: Sequence[str]) -> list[SyncOutcome]:
        catalog = self._catalog()
        outcomes = []
        for day in sorted(set(dates)):
            outcomes.append(self.sync_date(day, catalog))
        return outcomes

    def pending_dates(self, lookback_trading_days: int, today: str | None = None) -> list[str]:
        """Catalog days (newest ``lookback`` days that have a usable version) - sync is idempotent so all are tried."""
        catalog = self._catalog()
        days = [day for day in sorted(catalog.get("datasets", {}).get("daily_kline", {}).get("dates", {}))
                if (today is None or day <= today) and self.best_catalog_version(day, catalog) is not None]
        return days[-max(1, int(lookback_trading_days)):]


def sync_with_retry(sink: DailyKlineTdSink, dates: Sequence[str], *, attempts: int = 3, backoff_seconds: float = 1.0,
                    sleep: Callable[[float], None] = time.sleep) -> list[SyncOutcome]:
    """Retry TdUnavailable per the caller's config; a verification failure is deterministic and raised at once."""
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return sink.sync_dates(dates)
        except TdUnavailable as exc:
            last = exc
            sink.store.close()
            if attempt + 1 < attempts:
                sleep(backoff_seconds * (2 ** attempt))
    assert last is not None
    raise last
