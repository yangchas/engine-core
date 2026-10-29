"""Versioned TD sink for ``limit_up_detail_v1``; it never creates a database (C611)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .td_sink import (SHANGHAI, TaosStore, TdUnavailable, TdVerifyError, _SYMBOL, _check_date, _literal,
                      _parse_instant, trade_day_ts_us, validate_database_name)

SUPER_TABLE = "limit_up_detail_v1"
META_TABLE = "limit_up_detail_versions"
SCHEMA_VERSION = 1
INSERT_CHUNK = 200

# Types follow C610's field table and the observed 23-position raw response. Unverified semantic candidates stay NULL.
COLUMN_TYPES = (
    ("trade_date", "VARCHAR(10)"), ("name", "VARCHAR(64)"),
    ("kp_02_raw", "BIGINT"), ("kp_03_raw", "VARCHAR(64)"),
    ("limit_time_epoch", "BIGINT"), ("first_seal_time", "VARCHAR(16)"),
    ("kp_05_raw", "VARCHAR(256)"), ("kp_06_raw", "BIGINT"), ("kp_07_raw", "BIGINT"),
    ("kp_08_raw", "BIGINT"), ("kp_09_raw", "BIGINT"), ("kp_10_raw", "BIGINT"),
    ("trade_amount_yuan", "DOUBLE"), ("reason_plates_text", "VARCHAR(512)"),
    ("kp_13_raw", "BIGINT"), ("kp_14_raw", "DOUBLE"), ("lb_days", "INT"),
    ("kp_16_raw", "BIGINT"), ("kp_17_raw", "DOUBLE"), ("kp_18_raw", "VARCHAR(128)"),
    ("pool_plate_code", "VARCHAR(16)"), ("kp_20_raw", "BIGINT"),
    ("price", "DOUBLE"), ("pct_chg", "DOUBLE"),
    ("close_pct", "DOUBLE"), ("seal_time_text", "VARCHAR(32)"), ("reason_category", "VARCHAR(256)"),
    ("open_count", "INT"), ("seal_amount_yuan", "DOUBLE"), ("max_seal_amount_yuan", "DOUBLE"),
    ("main_net_yuan", "DOUBLE"), ("main_buy_yuan", "DOUBLE"), ("turnover_yuan", "DOUBLE"),
    ("main_sell_signed_yuan", "DOUBLE"), ("actual_float_mktcap_yuan", "DOUBLE"),
    ("actual_turnover_pct", "DOUBLE"), ("amplitude_pct", "DOUBLE"), ("streak_text", "VARCHAR(64)"),
    ("daily_type", "INT"), ("plate_count_candidate", "INT"), ("last_seal_time", "VARCHAR(16)"),
    ("streak_n_snapshot", "INT"), ("streak_source_version", "VARCHAR(24)"),
    ("reason_text_raw", "VARCHAR(2048)"), ("theme", "VARCHAR(512)"),
    ("parser_version", "VARCHAR(40)"), ("reason_zscodes_json", "VARCHAR(512)"),
    ("main_reason_plate_code", "VARCHAR(16)"), ("reason_source_date", "VARCHAR(10)"),
    ("plate_limit_up_count", "INT"), ("limit_type_wencai", "VARCHAR(32)"),
    ("one_word_kline", "BOOL"), ("limit_type_check", "VARCHAR(24)"),
    ("source_quality", "VARCHAR(32)"), ("quality_json", "VARCHAR(1000)"),
    ("source", "VARCHAR(32)"), ("provider", "VARCHAR(64)"), ("available_at", "VARCHAR(40)"),
    ("source_available_at", "VARCHAR(40)"), ("first_observed_at", "VARCHAR(40)"),
    ("captured_at", "VARCHAR(40)"), ("reason_captured_at", "VARCHAR(40)"),
)
DATA_COLUMNS = tuple(name for name, _type in COLUMN_TYPES)
META_COLUMNS = ("ts", "trade_date", "version", "business_status", "source", "provider", "quality", "row_count",
                "sha256", "source_available_at", "first_observed_at", "captured_at", "synced_at", "schema_version")
_META_WIDTHS = {"trade_date": 10, "version": 24, "business_status": 16, "source": 32, "provider": 64,
                "quality": 4000, "sha256": 64, "source_available_at": 40, "first_observed_at": 40,
                "captured_at": 40, "synced_at": 40}


def ddl_statements(database: str) -> list[str]:
    db = validate_database_name(database)
    columns = ", ".join(f"{name} {type_}" for name, type_ in COLUMN_TYPES)
    meta = ", ".join(f"{name} {_meta_type(name)}" for name in META_COLUMNS if name != "ts")
    return [
        f"CREATE STABLE IF NOT EXISTS {db}.{SUPER_TABLE} (ts TIMESTAMP, {columns}, version VARCHAR(24)) "
        "TAGS (symbol VARCHAR(8))",
        f"CREATE TABLE IF NOT EXISTS {db}.{META_TABLE} (ts TIMESTAMP, {meta})",
    ]


def _meta_type(name: str) -> str:
    if name in {"row_count", "schema_version"}:
        return "INT"
    return f"VARCHAR({_META_WIDTHS.get(name, 64)})"


def _numeric(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f")


def _canonical(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return {"number": _numeric(value)}
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).isoformat(timespec="microseconds")
    return str(value)


def rows_sha256(rows: Mapping[str, Mapping[str, Any]]) -> str:
    canonical = [[symbol, *(_canonical(rows[symbol].get(column)) for column in DATA_COLUMNS)]
                 for symbol in sorted(rows)]
    body = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _validate_rows(trade_date: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not _SYMBOL.fullmatch(symbol) or len(symbol) != 6:
            raise TdVerifyError("invalid_limit_up_detail_symbol")
        if str(row.get("trade_date") or "") != trade_date:
            raise TdVerifyError("limit_up_detail_row_date_mismatch")
        if symbol in mapped:
            raise TdVerifyError("duplicate_limit_up_detail_symbol")
        mapped[symbol] = {column: row.get(column) for column in DATA_COLUMNS}
    return mapped


def _meta_ts(trade_date: str, version: str, captured_at: str,
             existing: Sequence[Mapping[str, Any]]) -> int:
    """Keep one TD metadata row per source version, with distinct timestamps across versions."""
    for item in existing:
        if str(item.get("version") or "") == version:
            return int(item["ts"])
    moment = _parse_instant(captured_at)
    ts = int(moment.timestamp() * 1_000_000) if moment else trade_day_ts_us(trade_date)
    taken = {int(item["ts"]) for item in existing if item.get("ts") is not None}
    while ts in taken:
        ts += 1
    return ts


class TaosLimitUpDetailStore(TaosStore):
    """Existing TD database only; CREATE DATABASE is intentionally absent from this sink."""

    def schema_exists(self) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute("SHOW DATABASES")
            names = {str(row[0]) for row in cursor.fetchall()}
        except Exception as exc:
            self.close()
            raise TdUnavailable("td_statement_failed:" + type(exc).__name__) from exc
        if self.database not in names:
            return False
        return bool(self._run(f"DESCRIBE {self.database}.{SUPER_TABLE}", missing_ok=True)) and bool(
            self._run(f"DESCRIBE {self.database}.{META_TABLE}", missing_ok=True))

    def ensure_schema(self) -> None:
        cursor = self._cursor()
        try:
            cursor.execute("SHOW DATABASES")
            names = {str(row[0]) for row in cursor.fetchall()}
        except Exception as exc:
            self.close()
            raise TdUnavailable("td_statement_failed:" + type(exc).__name__) from exc
        if self.database not in names:
            raise TdUnavailable("td_database_not_preexisting")
        for statement in ddl_statements(self.database):
            self._run(statement)

    def existing_day_counts(self) -> dict[str, int]:
        rows = self._run(f"SELECT trade_date, COUNT(*) FROM {self.database}.{SUPER_TABLE} GROUP BY trade_date",
                         missing_ok=True)
        out: dict[str, int] = {}
        for day, count in rows:
            text = str(day or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                out[text] = int(count or 0)
        return out

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        _check_date(trade_date)
        selection = ", ".join(["CAST(ts AS BIGINT)", *META_COLUMNS[1:]])
        rows = self._run(f"SELECT {selection} FROM {self.database}.{META_TABLE} WHERE trade_date = '{trade_date}' "
                         "ORDER BY ts", missing_ok=True)
        result = []
        for raw in rows:
            item = dict(zip(META_COLUMNS, raw))
            try:
                item["quality"] = json.loads(item.get("quality") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["quality"] = {}
            result.append(item)
        return result

    def write_version(self, meta: Mapping[str, Any]) -> None:
        values = dict(meta)
        values["quality"] = json.dumps(values.get("quality") or {}, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
        if len(values["quality"].encode("utf-8")) > 3900:
            values["quality"] = json.dumps({"truncated": True})
        fields = ", ".join(META_COLUMNS)
        self._run(f"INSERT INTO {self.database}.{META_TABLE} ({fields}) VALUES (" +
                  ", ".join(_literal(values.get(name)) for name in META_COLUMNS) + ")")

    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        _check_date(trade_date)
        ts = trade_day_ts_us(trade_date)
        self._run(f"DELETE FROM {self.database}.{SUPER_TABLE} WHERE ts = {ts}", missing_ok=True)
        symbols = sorted(mapped)
        for start in range(0, len(symbols), INSERT_CHUNK):
            statements = []
            for symbol in symbols[start:start + INSERT_CHUNK]:
                if not _SYMBOL.fullmatch(symbol) or len(symbol) != 6:
                    raise TdVerifyError("invalid_limit_up_detail_symbol")
                row = mapped[symbol]
                values = ", ".join(_literal(row.get(name)) for name in DATA_COLUMNS)
                statements.append(f"{self.database}.lu_{symbol} USING {self.database}.{SUPER_TABLE} "
                                  f"TAGS('{symbol}') VALUES ({ts}, {values}, {_literal(version)})")
            if statements:
                self._run("INSERT INTO " + " ".join(statements))

    def read_day_rows(self, trade_date: str, symbols: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        _check_date(trade_date)
        if symbols is not None and not symbols:
            return {}
        symbol_filter = ""
        if symbols is not None:
            selected = sorted({str(symbol) for symbol in symbols if _SYMBOL.fullmatch(str(symbol)) and len(str(symbol)) == 6})
            if not selected:
                return {}
            symbol_filter = " AND symbol IN (" + ",".join("'" + symbol + "'" for symbol in selected) + ")"
        columns = ", ".join(DATA_COLUMNS)
        rows = self._run(f"SELECT symbol, {columns}, version FROM {self.database}.{SUPER_TABLE} "
                         f"WHERE ts = {trade_day_ts_us(trade_date)}{symbol_filter}", missing_ok=True)
        return {str(row[0]): {**dict(zip(DATA_COLUMNS, row[1:1 + len(DATA_COLUMNS)])), "version": row[-1]}
                for row in rows}

    def store_day(self, trade_date: str, rows: Sequence[Mapping[str, Any]], *, captured_at: str,
                  first_observed_at: str, quality: Mapping[str, Any], business_status: str) -> dict[str, Any]:
        _check_date(trade_date)
        mapped = _validate_rows(trade_date, rows)
        if not mapped:
            raise TdVerifyError("limit_up_detail_empty_is_unknown")
        sha = rows_sha256(mapped)
        version = "sha256-" + sha[:16]
        self.replace_day(trade_date, version, mapped)
        actual = self.read_day_rows(trade_date)
        actual_data = {symbol: {column: row.get(column) for column in DATA_COLUMNS} for symbol, row in actual.items()}
        if len(actual) != len(mapped) or rows_sha256(actual_data) != sha:
            raise TdVerifyError("limit_up_detail_td_readback_mismatch")
        for symbol in sorted(mapped)[: min(5, len(mapped))]:
            for column in ("trade_date", "name", "lb_days", "limit_time_epoch", "price", "pct_chg",
                           "trade_amount_yuan", "kp_09_raw", "main_buy_yuan", "turnover_yuan",
                           "reason_text_raw", "theme", "main_reason_plate_code"):
                if _canonical(mapped[symbol].get(column)) != _canonical(actual[symbol].get(column)):
                    raise TdVerifyError("limit_up_detail_sample_readback_mismatch:" + column)
        versions = self.read_versions(trade_date)
        first_seen = first_observed_at or captured_at
        if versions:
            prior = [str(item.get("first_observed_at") or "") for item in versions if item.get("first_observed_at")]
            if prior:
                first_seen = min(prior + [first_seen])
        meta_ts = _meta_ts(trade_date, version, captured_at, versions)
        expected_quality = dict(quality)
        self.write_version({
            "ts": meta_ts, "trade_date": trade_date, "version": version,
            "business_status": business_status, "source": "kaipan", "provider": "getHisBans+getBanReasons",
            "quality": expected_quality, "row_count": len(mapped), "sha256": sha,
            "source_available_at": None, "first_observed_at": first_seen, "captured_at": captured_at,
            "synced_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"), "schema_version": SCHEMA_VERSION,
        })
        version_readback = next((item for item in self.read_versions(trade_date)
                                 if str(item.get("version") or "") == version
                                 and int(item.get("ts") or -1) == meta_ts), None)
        if (version_readback is None or int(version_readback.get("row_count") or -1) != len(mapped)
                or str(version_readback.get("sha256") or "") != sha
                or str(version_readback.get("trade_date") or "") != trade_date
                or str(version_readback.get("source") or "") != "kaipan"
                or str(version_readback.get("provider") or "") != "getHisBans+getBanReasons"
                or version_readback.get("quality") != expected_quality):
            raise TdVerifyError("limit_up_detail_version_readback_mismatch")
        return {"trade_date": trade_date, "row_count": len(mapped), "sha256": sha, "version": version,
                "first_observed_at": first_seen, "version_metadata_ts": meta_ts,
                "version_row_verified": True}
