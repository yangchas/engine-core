"""Core-owned TD storage of the ``limit_up_pool`` dataset (C580), same sink pattern as ``daily_kline`` (C567).

Cold data -> TD (design/24 section 2).  The local catalog partition is the transitional versioned ledger written
first; this module copies the best catalog version of a day into ``<database>.limit_up_pool_v1``, reads it back and
compares EVERY row and column (a pool has well under 200 rows), and records the version in
``<database>.limit_up_pool_versions``.  The database is the one of ``daily_kline`` (``core_data``); a throw-away
``core_dev_*`` database is used in tests and acceptance runs.  ``market_data1`` is refused (td_sink rules).

TD column mapping (the only place where catalog rows become TD columns):
    ts (TIMESTAMP)      trade date D-1 00:00:00 +08:00, microseconds       <- partition trade_date
    tag symbol          6-char code                                         <- row.symbol
    every other column  same name as the catalog row key, type by COLUMN_KIND:
                        str -> VARCHAR(n), int -> BIGINT, float -> DOUBLE, bool -> BOOL; missing -> NULL
    version             catalog version (sha256-<16>) the row came from
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .limit_pool import COLUMN_KIND, COLUMNS, DATASET, REQUIRED_COLUMNS, SCHEMA_VERSION
from .td_sink import (SHANGHAI, SyncOutcome, TaosStore, TdUnavailable, TdVerifyError, _SYMBOL, _check_date,
                      _literal, _parse_instant, pick_best, trade_day_ts_us, validate_database_name, version_rank)
from datetime import datetime
from .derived_daily import normalize_status

SUPER_TABLE = "limit_up_pool_v1"
META_TABLE = "limit_up_pool_versions"
# Columns kept in the stable beyond the tag; `date`/`trade_date` are the ts, `symbol` is the tag.
DATA_COLUMNS = tuple(name for name, *_ in COLUMNS if name not in {"symbol", "date", "trade_date"})
_VARCHAR_WIDTH = {"name": 64, "reason_category": 256, "reason_plates_text": 512, "wc_reason_category": 256,
                  "plate_code": 16, "seal_time_text": 32, "kp_18": 64, "wc_limit_type": 32, "wc_streak_text": 32,
                  "wc_first_limit_time": 16, "wc_last_limit_time": 16}
_META_COLUMNS = ("ts", "trade_date", "version", "business_status", "rank_level", "source", "provider", "degraded",
                 "quality", "row_count", "sha256", "available_at", "synced_at", "schema_version")


def _td_type(name: str) -> str:
    kind = COLUMN_KIND[name]
    return {"int": "BIGINT", "float": "DOUBLE", "bool": "BOOL"}.get(kind) or f"VARCHAR({_VARCHAR_WIDTH.get(name, 64)})"


def ddl_statements(database: str) -> list[str]:
    db = validate_database_name(database)
    columns = ", ".join(f"{name} {_td_type(name)}" for name in DATA_COLUMNS)
    return [
        f"CREATE DATABASE IF NOT EXISTS {db} PRECISION 'us' KEEP 3650d DURATION 90d VGROUPS 2",
        f"CREATE STABLE IF NOT EXISTS {db}.{SUPER_TABLE} (ts TIMESTAMP, {columns}, version VARCHAR(24)) "
        "TAGS (symbol VARCHAR(8))",
        f"CREATE TABLE IF NOT EXISTS {db}.{META_TABLE} (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), "
        "business_status VARCHAR(16), rank_level INT, source VARCHAR(96), provider VARCHAR(32), degraded BOOL, "
        "quality VARCHAR(4000), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), synced_at VARCHAR(40), "
        "schema_version INT)",
    ]


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    kind = COLUMN_KIND[name]
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "int":
        return None if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            (isinstance(value, float) and not math.isfinite(value)) else int(round(value))
    if kind == "float":
        return None if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(float(value)) else float(value)
    return str(value)


def map_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Catalog rows -> {symbol: {td column: value}}; a repeated symbol: last wins (counted)."""
    mapped: dict[str, dict[str, Any]] = {}
    dropped = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not _SYMBOL.match(symbol):
            dropped += 1
            continue
        if symbol in mapped:
            dropped += 1
        mapped[symbol] = {name: _coerce(name, row.get(name)) for name in DATA_COLUMNS}
    return mapped, dropped


class PoolTaosStore(TaosStore):
    """Real TDengine store for the pool tables (connection handling inherited from ``TaosStore``)."""

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
        return bool(self._run(f"SELECT COUNT(*) FROM {self.database}.{META_TABLE}", missing_ok=True)) and \
            self._run(f"DESCRIBE {self.database}.{SUPER_TABLE}", missing_ok=True) != []

    def ensure_schema(self) -> None:
        for statement in ddl_statements(self.database):
            self._run(statement)

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        _check_date(trade_date)
        rows = self._run(f"SELECT CAST(ts AS BIGINT), {', '.join(_META_COLUMNS[1:])} FROM {self.database}."
                         f"{META_TABLE} WHERE trade_date = '{trade_date}' ORDER BY ts", missing_ok=True)
        out = []
        for row in rows:
            item = dict(zip(_META_COLUMNS, row))
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
        self._run(f"INSERT INTO {self.database}.{META_TABLE} ({', '.join(_META_COLUMNS)}) VALUES ("
                  + ", ".join(_literal(values.get(column)) for column in _META_COLUMNS) + ")")

    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        _check_date(trade_date)
        ts = trade_day_ts_us(trade_date)
        self._run(f"DELETE FROM {self.database}.{SUPER_TABLE} WHERE ts = {ts}", missing_ok=True)
        for symbol, values in sorted(mapped.items()):
            literal = ", ".join(_literal(values.get(name)) for name in DATA_COLUMNS)
            self._run(f"INSERT INTO {self.database}.p_{symbol} USING {self.database}.{SUPER_TABLE} "
                      f"TAGS('{symbol}') VALUES ({ts}, {literal}, {_literal(version)})")

    def read_day_rows(self, trade_date: str) -> dict[str, dict[str, Any]]:
        _check_date(trade_date)
        rows = self._run(f"SELECT symbol, {', '.join(DATA_COLUMNS)}, version FROM {self.database}.{SUPER_TABLE} "
                         f"WHERE ts = {trade_day_ts_us(trade_date)}", missing_ok=True)
        return {row[0]: {**dict(zip(DATA_COLUMNS, row[1:1 + len(DATA_COLUMNS)])), "version": row[-1]} for row in rows}

    def day_counts(self, dates: Sequence[str]) -> dict[str, int]:
        out = {}
        for day in dates:
            _check_date(day)
            rows = self._run(f"SELECT COUNT(*) FROM {self.database}.{SUPER_TABLE} "
                             f"WHERE ts = {trade_day_ts_us(day)}", missing_ok=True)
            out[day] = int(rows[0][0]) if rows else 0
        return out


class MemoryPoolStore:
    """In-memory fake with the same semantics (unit tests; ``fail='unavailable'`` simulates an outage)."""

    def __init__(self) -> None:
        self.created = False
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.days: dict[str, dict[str, dict[str, Any]]] = {}
        self.fail: str | None = None
        self.corrupt: str | None = None   # column to corrupt on replace_day (mutation tests)

    def _guard(self) -> None:
        if self.fail == "unavailable":
            raise TdUnavailable("fake_td_down")

    def schema_exists(self) -> bool:
        self._guard()
        return self.created

    def ensure_schema(self) -> None:
        self._guard()
        self.created = True

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        self._guard()
        return [dict(item) for item in self.versions.get(trade_date, [])]

    def write_version(self, meta: Mapping[str, Any]) -> None:
        self._guard()
        bucket = self.versions.setdefault(str(meta["trade_date"]), [])
        bucket[:] = [item for item in bucket if item["ts"] != meta["ts"]]
        bucket.append(dict(meta))
        bucket.sort(key=lambda item: item["ts"])

    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        self._guard()
        stored = {symbol: {**dict(values), "version": version} for symbol, values in mapped.items()}
        if self.corrupt and stored:
            first = sorted(stored)[0]
            stored[first][self.corrupt] = None if stored[first].get(self.corrupt) is not None else "x"
        self.days[trade_date] = stored

    def read_day_rows(self, trade_date: str) -> dict[str, dict[str, Any]]:
        self._guard()
        return {symbol: dict(row) for symbol, row in self.days.get(trade_date, {}).items()}

    def day_counts(self, dates: Sequence[str]) -> dict[str, int]:
        self._guard()
        return {day: len(self.days.get(day, {})) for day in dates}

    def close(self) -> None:
        pass


def catalog_candidates(catalog: Mapping[str, Any], trade_date: str) -> list[dict[str, Any]]:
    """Catalog versions of the day with the manifest merged: only contract schema >= 2 with rows qualify."""
    versions = catalog.get("datasets", {}).get(DATASET, {}).get("dates", {}).get(trade_date) or []
    out = []
    for position, entry in enumerate(versions):
        if not isinstance(entry, Mapping) or not entry.get("manifest"):
            continue
        try:
            manifest = json.loads(Path(str(entry["manifest"])).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, Mapping) or int(manifest.get("schema_version") or 0) < SCHEMA_VERSION:
            continue
        merged = {**manifest, **entry, "needs_upgrade": False, "_position": position}
        if int(merged.get("row_count") or 0) > 0 and merged.get("version"):
            out.append(merged)
    return out


class LimitPoolTdSink:
    def __init__(self, store: Any, root: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.root = Path(root).expanduser()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _catalog(self) -> dict[str, Any]:
        from .storage import ArtifactCatalog
        return ArtifactCatalog(self.root).read()

    def pending_dates(self, lookback: int = 10, today: str | None = None) -> list[str]:
        catalog = self._catalog()
        days = [day for day in sorted(catalog.get("datasets", {}).get(DATASET, {}).get("dates", {}))
                if (today is None or day <= today) and catalog_candidates(catalog, day)]
        return days[-max(1, int(lookback)):]

    def verify_day(self, trade_date: str, best: Mapping[str, Any],
                   mapped: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Read the whole day back and compare every row and column with what the catalog says."""
        got = self.store.read_day_rows(trade_date)
        problems: list[str] = []
        if set(got) != set(mapped):
            problems.append(f"symbols: catalog_only={sorted(set(mapped) - set(got))[:5]} "
                            f"td_only={sorted(set(got) - set(mapped))[:5]}")
        compared = 0
        for symbol, want_row in mapped.items():
            have_row = got.get(symbol)
            if have_row is None:
                continue
            if have_row.get("version") != best["version"]:
                problems.append(f"{symbol}.version: catalog={best['version']} td={have_row.get('version')}")
                continue
            for column in DATA_COLUMNS:
                want, have = want_row.get(column), have_row.get(column)
                compared += 1
                if want is None or have is None:
                    same = want is None and have is None
                elif isinstance(want, float):
                    same = abs(want - float(have)) <= 1e-9 * max(1.0, abs(want))
                else:
                    same = want == have
                if not same:
                    problems.append(f"{symbol}.{column}: catalog={want!r} td={have!r}")
                    break
        if problems:
            raise TdVerifyError("td_readback_mismatch:" + trade_date + ":" + "; ".join(problems[:4]))
        return {"rows": len(mapped), "cells_compared": compared}

    def _meta(self, trade_date: str, best: Mapping[str, Any], dropped: int, ts: int) -> dict[str, Any]:
        quality = dict(best.get("quality") or {}) if isinstance(best.get("quality"), Mapping) else {}
        quality["dropped_rows"] = dropped
        return {"ts": ts, "trade_date": trade_date, "version": str(best["version"]),
                "business_status": normalize_status(best.get("business_status")),
                "rank_level": version_rank(best.get("business_status"), False)[0],
                "source": str(best.get("source") or "")[:96], "provider": str(best.get("provider") or "")[:32],
                "degraded": bool(best.get("degraded")), "quality": quality,
                "row_count": int(best.get("row_count") or 0), "sha256": str(best.get("sha256") or ""),
                "available_at": str(best.get("available_at") or ""), "synced_at": self.clock().isoformat(),
                "schema_version": SCHEMA_VERSION}

    @staticmethod
    def _meta_ts(trade_date: str, best: Mapping[str, Any], existing: Sequence[Mapping[str, Any]]) -> int:
        for item in existing:
            if item.get("version") == best["version"]:
                return int(item["ts"])
        moment = _parse_instant(best.get("available_at") or best.get("committed_at"))
        ts = int(moment.timestamp() * 1_000_000) if moment else trade_day_ts_us(trade_date)
        taken = {int(item["ts"]) for item in existing}
        while ts in taken:
            ts += 1
        return ts

    def write_partition(self, trade_date: str, rows: Sequence[Mapping[str, Any]],
                        manifest: Mapping[str, Any]) -> SyncOutcome:
        """Write the producer's just-committed partition without rereading it from the catalog."""
        if str(manifest.get("trade_date") or trade_date) != trade_date:
            raise TdVerifyError("td_trade_date_mismatch:" + trade_date)
        if not manifest.get("version") or int(manifest.get("row_count") or -1) != len(rows):
            raise TdVerifyError("td_partition_manifest_mismatch:" + trade_date)
        if not self.store.schema_exists():
            self.store.ensure_schema()
        existing = self.store.read_versions(trade_date)
        current = pick_best(existing)
        current_key = version_rank(current.get("business_status"), False) if current else None
        best_key = version_rank(manifest.get("business_status"), False)
        if current is not None and current_key > best_key:
            return SyncOutcome(trade_date, "skipped_lower", str(current["version"]), current.get("business_status"),
                                   int(current.get("row_count") or 0),
                                   {"partition_version": manifest["version"], "reason": "td_has_higher_quality_version"})
        if current is not None and current.get("version") == manifest.get("version") and \
                self.store.day_counts([trade_date]).get(trade_date) == int(manifest.get("row_count") or -1):
            return SyncOutcome(trade_date, "unchanged", str(manifest["version"]), current.get("business_status"),
                               int(current["row_count"]))
        mapped, dropped = map_rows(rows)
        if not mapped:
            raise TdVerifyError("td_partition_maps_to_zero_rows:" + trade_date)
        self.store.replace_day(trade_date, str(manifest["version"]), mapped)
        report = self.verify_day(trade_date, manifest, mapped)
        meta = self._meta(trade_date, manifest, dropped, self._meta_ts(trade_date, manifest, existing))
        self.store.write_version(meta)
        readback = next((item for item in self.store.read_versions(trade_date)
                         if str(item.get("version")) == str(manifest["version"])), None)
        if (readback is None or str(readback.get("sha256") or "") != str(manifest.get("sha256") or "")
                or int(readback.get("row_count") or -1) != len(mapped)
                or self.store.day_counts([trade_date]).get(trade_date) != len(mapped)):
            raise TdVerifyError("td_version_readback_mismatch:" + trade_date)
        return SyncOutcome(trade_date, "synced", str(manifest["version"]), meta["business_status"], len(mapped),
                           {"sha256": str(manifest.get("sha256") or ""), "cells_compared": report["cells_compared"],
                            "version_row_verified": True})

    def sync_date(self, trade_date: str, catalog: Mapping[str, Any] | None = None) -> SyncOutcome:
        catalog = catalog if catalog is not None else self._catalog()
        best = pick_best(catalog_candidates(catalog, trade_date))
        if best is None:
            return SyncOutcome(trade_date, "no_catalog_version")
        from .storage import ArtifactCatalog
        rows = ArtifactCatalog(self.root).read_rows(Path(str(best["manifest"])))
        return self.write_partition(trade_date, rows, best)

    def sync_dates(self, dates: Sequence[str]) -> list[SyncOutcome]:
        catalog = self._catalog()
        return [self.sync_date(day, catalog) for day in sorted(set(dates))]

    def td_has(self, trade_date: str) -> bool:
        """True when TD holds a complete day (version row present and row count equal)."""
        current = pick_best(self.store.read_versions(trade_date))
        return current is not None and self.store.day_counts([trade_date]).get(trade_date) == int(
            current.get("row_count") or -1)
