"""Kaipan daily DDE parser, gap sync and TD/Redis sinks (C600)."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .derived_daily import Partition

DATASET = "daily_dde"
SUPER_TABLE = "daily_dde_v1"
DATA_COLUMNS = ("ddje", "large_net", "ddx", "ddy", "ddz")
FIELD_UNITS = {"ddje": "yuan", "large_net": "not_provided_by_source",
               "ddx": "not_provided_by_source", "ddy": "not_provided_by_source",
               "ddz": "not_provided_by_source"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL_RE = re.compile(r"^\d{6}$")


class DdeError(RuntimeError):
    pass


class DdeStaleResponse(DdeError):
    pass


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        text = value.date().isoformat()
    elif isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value or "").strip()
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        elif len(text) >= 10:
            text = text[:10]
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _field(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def build_daily_dde(raw_response: Any, *, symbol: str, target_date: str, max_days: int = 20) -> Partition:
    """Build at most ``max_days`` rows from Kaipan's echoed Date column, preserving absent values as NULL."""
    code = str(symbol).strip().zfill(6)
    if not _SYMBOL_RE.fullmatch(code):
        raise ValueError("invalid_dde_symbol")
    target = _date_text(target_date)
    if target is None:
        raise ValueError("invalid_dde_target_date")
    if isinstance(max_days, bool) or int(max_days) < 1:
        raise ValueError("max_days_must_be_positive")
    if not isinstance(raw_response, Mapping):
        raise DdeError("kaipan_dde_response_not_object")
    errcode = str(raw_response.get("errcode", "0"))
    if errcode not in {"", "0", "None"}:
        raise DdeError("kaipan_dde_errorcode:" + errcode[:40])
    raw_dates = raw_response.get("Date")
    if not isinstance(raw_dates, (list, tuple)):
        raise DdeError("kaipan_dde_date_column_missing")
    rows_by_date: dict[str, dict[str, Any]] = {}
    source_dates = []
    for index, raw_day in enumerate(raw_dates):
        day = _date_text(raw_day)
        if day is None or day > target:
            continue
        source_dates.append(day)
        row = {"symbol": code, "date": day}
        for name, aliases in (("ddje", ("DDJE", "ddje")),
                              ("large_net", ("LARGE_NET", "large_net")),
                              ("ddx", ("DDX", "ddx")),
                              ("ddy", ("DDY", "ddy")),
                              ("ddz", ("DDZ", "ddz"))):
            values = _field(raw_response, *aliases)
            value = values[index] if isinstance(values, (list, tuple)) and index < len(values) else None
            row[name] = _number(value)
        rows_by_date[day] = row
    if not source_dates:
        raise DdeStaleResponse("kaipan_dde_has_no_date_at_or_before_target:" + target)
    latest = max(source_dates)
    if latest < target:
        raise DdeStaleResponse(f"kaipan_dde_stale:latest={latest}:target={target}")
    if target not in rows_by_date:
        raise DdeStaleResponse(f"kaipan_dde_target_date_missing:target={target}:latest={latest}")
    rows = [rows_by_date[day] for day in sorted(rows_by_date)[-int(max_days):]]
    raw_digest = hashlib.sha256(json.dumps(raw_response, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {
        "schema_version": 1, "dataset": DATASET, "trade_date": target, "provider": "kaipan", "source": "kaipan",
        "symbol": code, "as_of": now, "available_at": now, "source_dates": {"date": [row["date"] for row in rows]},
        "field_units": dict(FIELD_UNITS), "unit_evidence": {
            "ddje": "legacy TD same-date comparison; Kaipan field documentation was not present in the response",
            "large_net": "Kaipan response has no large_net column; legacy writer did not populate it",
            "ddx": "Kaipan response has no DDX column", "ddy": "Kaipan response has no DDY column",
            "ddz": "Kaipan response has no DDZ column"},
        "row_count": len(rows), "raw_response_sha256": raw_digest,
        "version": raw_digest[:24], "quality": {"missing_values": {
            column: sum(row[column] is None for row in rows) for column in DATA_COLUMNS}},
    }
    return Partition(rows, manifest)


def _ts_us(day: str) -> int:
    from .td_sink import trade_day_ts_us
    return int(trade_day_ts_us(day))


def _sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return "NULL"
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class TaosDdeStore:
    """TD store restricted to core_data and pre-created core_dev_* validation databases."""

    def __init__(self, database: str, *, connect: Callable[[], Any] | None = None):
        self.database = str(database)
        if self.database != "core_data" and not re.fullmatch(r"core_dev_[a-z0-9_]{1,24}", self.database):
            raise ValueError("invalid_dde_td_database")
        self._connect_fn = connect or self._default_connect
        self._conn = None

    @staticmethod
    def _default_connect():
        if not os.environ.get("TD_CONFIG"):
            raise DdeError("TD_CONFIG_required")
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
            except Exception as exc:
                raise DdeError("td_connect_failed:" + type(exc).__name__) from exc
        return self._conn.cursor()

    def _run(self, sql: str) -> list[tuple]:
        cursor = self._cursor()
        try:
            cursor.execute(sql)
            try:
                return list(cursor.fetchall())
            except Exception:
                return []
        except Exception as exc:
            self.close()
            raise DdeError("td_statement_failed:" + type(exc).__name__ + ":" + str(exc)[:160]) from exc

    def database_exists(self) -> bool:
        rows = self._run("SHOW DATABASES")
        return self.database in {str(row[0]) for row in rows}

    def ensure_schema(self) -> str:
        if not self.database_exists():
            raise DdeError("td_database_must_already_exist:" + self.database)
        ddl = (f"CREATE STABLE IF NOT EXISTS {self.database}.{SUPER_TABLE} "
               "(ts TIMESTAMP, ddje DOUBLE, large_net DOUBLE, ddx DOUBLE, ddy DOUBLE, ddz DOUBLE, "
               "version VARCHAR(24)) TAGS (symbol VARCHAR(8))")
        self._run(ddl)
        return ddl

    def latest_watermarks(self) -> dict[str, str]:
        rows = self._run(f"SELECT symbol, LAST(ts) FROM {self.database}.{SUPER_TABLE} GROUP BY symbol")
        return {str(symbol): _date_text(value) for symbol, value in rows if _date_text(value)}

    def legacy_latest_date(self) -> str | None:
        rows = self._run("SELECT LAST(ts) FROM market_data1.daily_dde")
        return _date_text(rows[0][0]) if rows else None

    def write_and_verify(self, symbol: str, rows: Sequence[Mapping[str, Any]], version: str) -> int:
        if not rows:
            return 0
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("invalid_dde_symbol")
        minimum, maximum = min(row["date"] for row in rows), max(row["date"] for row in rows)
        values = []
        for row in rows:
            fields = [_ts_us(row["date"]), *[_sql(row.get(column)) for column in DATA_COLUMNS], _sql(version)]
            values.append("(" + ", ".join(str(value) for value in fields) + ")")
        # A single TD INSERT statement is the per-symbol batch boundary; read-back gates success.
        child_table = f"{SUPER_TABLE}_{symbol}"
        statement = (f"INSERT INTO {self.database}.{child_table} USING {self.database}.{SUPER_TABLE} "
                     f"TAGS('{symbol}') VALUES " + " ".join(values))
        self._run(statement)
        readback = self._run(
            f"SELECT ts, ddje, large_net, ddx, ddy, ddz, version FROM {self.database}.{child_table} "
            f"WHERE ts >= {_ts_us(minimum)} AND ts <= {_ts_us(maximum)}")
        got: dict[str, tuple] = {}
        for item in readback:
            day = _date_text(item[0])
            if day:
                got[day] = tuple(item[1:])
        mismatches = []
        for row in rows:
            item = got.get(row["date"])
            if item is None:
                mismatches.append(row["date"] + ":missing")
                continue
            for index, column in enumerate(DATA_COLUMNS):
                want, have = row.get(column), item[index]
                equal = want is None and have is None
                if want is not None and have is not None:
                    equal = math.isclose(float(want), float(have), rel_tol=1e-12, abs_tol=1e-8)
                if not equal:
                    mismatches.append(f"{row['date']}:{column}")
            if str(item[-1]) != version:
                mismatches.append(row["date"] + ":version")
        if mismatches:
            raise DdeError("td_readback_mismatch:" + ",".join(mismatches[:5]))
        return len(rows)

    def compare_legacy(self, symbols: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not symbols or not rows:
            return {"pairs": 0, "equal": 0, "different": 0, "unmatched": 0, "columns": {}, "reasons": {}}
        days = [row["date"] for row in rows]
        first = min(days)
        after_last = (date.fromisoformat(max(days)) + timedelta(days=1)).isoformat()
        quoted = ",".join("'" + symbol + "'" for symbol in sorted(set(symbols)) if _SYMBOL_RE.fullmatch(symbol))
        legacy = self._run("SELECT symbol, ts, ddje, large_net, ddx, ddy, ddz FROM market_data1.daily_dde "
                           f"WHERE ts >= '{first} 00:00:00' AND ts < '{after_last} 00:00:00' "
                           f"AND symbol IN ({quoted})")
        old = {}
        for item in legacy:
            day = _date_text(item[1])
            if day:
                old[(str(item[0]), day)] = dict(zip(DATA_COLUMNS, item[2:]))
        columns = {name: {"equal": 0, "different": 0, "missing_legacy": 0} for name in DATA_COLUMNS}
        reasons: dict[str, int] = {}
        equal = different = unmatched = 0
        for row in rows:
            prior = old.get((str(row["symbol"]), row["date"]))
            if prior is None:
                unmatched += 1
                reasons["no_legacy_row"] = reasons.get("no_legacy_row", 0) + 1
                continue
            row_equal = True
            for name in DATA_COLUMNS:
                want, have = row.get(name), prior.get(name)
                if want is None and have is None:
                    columns[name]["equal"] += 1
                elif want is None:
                    columns[name]["different"] += 1
                    row_equal = False
                    reason = "source_field_not_returned_legacy_default_zero" if _number(have) == 0 else "source_field_not_returned"
                    reasons[reason + ":" + name] = reasons.get(reason + ":" + name, 0) + 1
                elif have is None:
                    columns[name]["missing_legacy"] += 1
                    row_equal = False
                elif math.isclose(float(want), float(have), rel_tol=1e-6, abs_tol=1e-6):
                    columns[name]["equal"] += 1
                else:
                    columns[name]["different"] += 1
                    row_equal = False
                    reasons["numeric_difference:" + name] = reasons.get("numeric_difference:" + name, 0) + 1
            if row_equal:
                equal += 1
            else:
                different += 1
        return {"pairs": len(rows), "equal": equal, "different": different, "unmatched": unmatched,
                "columns": columns, "reasons": reasons}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _active_symbols(root: Path, trade_date: str) -> list[str]:
    from .storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    manifest = catalog.locate("symbol_universe", trade_date) or catalog.locate("symbol_universe")
    if manifest is None:
        raise DdeError("symbol_universe_missing")
    rows = catalog.read_rows(manifest)
    return sorted({str(row.get("symbol", "")).zfill(6) for row in rows
                   if str(row.get("symbol", "")).strip().isdigit()
                   and str(row.get("trade_status", "")).strip() == "1"})


def _fetch_with_retry(fetcher: Callable[[str, str], Any], symbol: str, target_date: str,
                      attempts: int, sleep: Callable[[float], None]) -> tuple[Partition | None, str | None, int]:
    last_error = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            return build_daily_dde(fetcher(symbol, target_date), symbol=symbol, target_date=target_date), None, attempt
        except Exception as exc:
            last_error = type(exc).__name__ + ":" + str(exc)[:180]
            if attempt < attempts:
                sleep(min(30.0, 0.5 * 2 ** (attempt - 1)))
    return None, last_error, attempts


def run_daily_dde_sync(symbols: Sequence[str] | None, target_date: str, *, database: str | None = None,
                       root: str | Path | None = None, max_workers: int = 3, attempts: int = 3,
                       redis_ttl_seconds: int = 86400, write_redis: bool = True,
                       skip_redis: bool = False, store: TaosDdeStore | None = None,
                       fetcher: Callable[[str, str], Any] | None = None,
                       sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    target = _date_text(target_date)
    if target is None:
        raise ValueError("invalid_dde_target_date")
    if isinstance(max_workers, bool) or not 1 <= int(max_workers) <= 4:
        raise ValueError("dde_max_workers_must_be_1_to_4")
    if isinstance(attempts, bool) or not 1 <= int(attempts) <= 5:
        raise ValueError("dde_attempts_must_be_1_to_5")
    if int(redis_ttl_seconds) < 1:
        raise ValueError("dde_redis_ttl_must_be_positive")
    base = Path(root or os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
    database_name = database or os.environ.get("CORE_TD_DATABASE") or "core_data"
    own = store is None
    store = store or TaosDdeStore(database_name)
    if fetcher is None:
        def fetcher(symbol: str, day: str):
            try:
                from pykaipan import pykaipan
            except ImportError:
                import importlib
                pykaipan = importlib.import_module("pykaipan.pykaipan")
            return pykaipan.getHisStockDDE(symbol, day)
    try:
        ddl = store.ensure_schema()
        before = store.latest_watermarks()
        requested = sorted(set(str(item).strip().zfill(6) for item in symbols)) if symbols is not None else _active_symbols(base, target)
        if any(not _SYMBOL_RE.fullmatch(item) for item in requested):
            raise ValueError("invalid_dde_symbol_list")
        requested = [symbol for symbol in requested if symbols is not None or before.get(symbol, "") < target]
        fetched: dict[str, Partition] = {}
        failed: dict[str, str] = {}
        request_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_workers)) as pool:
            futures = {pool.submit(_fetch_with_retry, fetcher, symbol, target, int(attempts), sleep): symbol
                       for symbol in requested}
            for future in concurrent.futures.as_completed(futures):
                symbol = futures[future]
                partition, error, used = future.result()
                request_count += used
                if partition is None:
                    failed[symbol] = error or "fetch_failed"
                else:
                    fetched[symbol] = partition
        successful_rows: list[dict[str, Any]] = []
        write_failures = {}
        written_symbols = 0
        for symbol in sorted(fetched):
            partition = fetched[symbol]
            try:
                count = store.write_and_verify(symbol, partition.rows, partition.manifest["version"])
                written_symbols += 1
                successful_rows.extend(partition.rows)
                if count != len(partition.rows):
                    raise DdeError("td_readback_row_count_mismatch")
            except Exception as exc:
                write_failures[symbol] = type(exc).__name__ + ":" + str(exc)[:180]
        failed.update(write_failures)
        after = store.latest_watermarks()
        comparison = store.compare_legacy(sorted(fetched), successful_rows)
        redis_result: dict[str, Any] = {"written": False, "readback_verified": False, "key": None, "ttl": None}
        if skip_redis:
            redis_result["skipped"] = True
        elif write_redis and successful_rows:
            redis_url = os.environ.get("REDIS_URL") or os.environ.get("CORE_REDIS_URL")
            if not redis_url:
                raise DdeError("REDIS_URL_required_for_dde_view")
            import redis
            key = "core:dde_ready:" + target
            view = {"dataset": DATASET, "date": target, "source": "kaipan", "field_units": FIELD_UNITS,
                    "rows": [row for row in successful_rows if row["date"] == target]}
            payload = json.dumps(view, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
            client.set(key, payload, ex=int(redis_ttl_seconds))
            readback = client.get(key)
            ttl = int(client.ttl(key))
            redis_result = {"written": True, "readback_verified": readback == payload,
                            "key": key, "ttl": ttl, "row_count": len(view["rows"])}
            if readback != payload or ttl < 1:
                raise DdeError("dde_redis_readback_failed")
        return {"status": "ok" if not failed else "partial", "database": store.database,
                "ddl": ddl, "requested_symbols": len(requested), "request_count": request_count,
                "written_symbols": written_symbols, "rows_written": len(successful_rows),
                "readback_verified": not write_failures and written_symbols == len(fetched),
                "readback_mismatches": len(write_failures),
                "known_missing": [{"symbol": symbol, "reason": reason} for symbol, reason in sorted(failed.items())],
                "watermarks_before": {symbol: before.get(symbol) for symbol in requested[:20]},
                "watermarks_after": {symbol: after.get(symbol) for symbol in requested[:20]},
                "watermark_counts": {"before": len(before), "after": len(after)},
                "legacy_comparison": comparison, "redis_readback_verified": bool(redis_result.get("readback_verified")),
                "redis": redis_result}
    finally:
        if own:
            store.close()
