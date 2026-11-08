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
DATA_COLUMNS = ("ddje",)
FIELD_UNITS = {"ddje": "yuan"}
DEFAULT_HISTORY_TRADING_DAYS = 250
DEFAULT_MAX_WORKERS = 1
# Assumes the SDK size requests the latest trading sessions; live verification is pending.
INCREMENTAL_FETCH_SIZE = 5
KAIPAN_REQUEST_TIMEOUT = (5.0, 15.0)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL_RE = re.compile(r"^\d{6}$")


class DdeError(RuntimeError):
    pass


class DdeStaleResponse(DdeError):
    pass


class DdeSyncRetryableError(DdeError):
    """The run persisted verified progress but did not complete every requested symbol."""

    retryable = True

    def __init__(self, summary: Mapping[str, Any]):
        self.summary = dict(summary)
        self.summary.setdefault("error_code", "DDE_SYNC_INCOMPLETE_RETRYABLE")
        super().__init__(self.summary["error_code"] + ":" + json.dumps(self.summary, ensure_ascii=False,
                                                                        separators=(",", ":")))


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


def build_daily_dde(raw_response: Any, *, symbol: str, target_date: str,
                    history_trading_days: int = DEFAULT_HISTORY_TRADING_DAYS) -> Partition:
    """Build the latest trading-day window from Kaipan's full-history response."""
    code = str(symbol).strip().zfill(6)
    if not _SYMBOL_RE.fullmatch(code):
        raise ValueError("invalid_dde_symbol")
    target = _date_text(target_date)
    if target is None:
        raise ValueError("invalid_dde_target_date")
    if isinstance(history_trading_days, bool) or not 1 <= int(history_trading_days) <= 5000:
        raise ValueError("history_trading_days_must_be_1_to_5000")
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
        values = raw_response.get("DDJE", raw_response.get("ddje"))
        value = values[index] if isinstance(values, (list, tuple)) and index < len(values) else None
        row = {"symbol": code, "date": day, "ddje": _number(value)}
        rows_by_date[day] = row
    if not source_dates:
        raise DdeStaleResponse("kaipan_dde_has_no_date_at_or_before_target:" + target)
    latest = max(source_dates)
    if latest < target:
        raise DdeStaleResponse(f"kaipan_dde_stale:latest={latest}:target={target}")
    if target not in rows_by_date:
        raise DdeStaleResponse(f"kaipan_dde_target_date_missing:target={target}:latest={latest}")
    # Date is the provider's trading calendar for this symbol; keep the newest
    # N returned sessions and write them newest-first for repair priority.
    rows = [rows_by_date[day] for day in sorted(rows_by_date, reverse=True)[:int(history_trading_days)]]
    raw_digest = hashlib.sha256(json.dumps(raw_response, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {
        "schema_version": 1, "dataset": DATASET, "trade_date": target, "provider": "kaipan", "source": "kaipan",
        "symbol": code, "as_of": now, "available_at": now, "source_dates": {"date": [row["date"] for row in rows]},
        "field_units": dict(FIELD_UNITS), "unit_evidence": {
            "ddje": "Kaipan getHisStockDDE response DDJE field; unit yuan matches legacy daily_dde"},
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
        from .td_sink import connect_td
        return connect_td()

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
        finally:
            self._release(cursor)

    def database_exists(self) -> bool:
        rows = self._run("SHOW DATABASES")
        return self.database in {str(row[0]) for row in rows}

    def ensure_schema(self) -> str:
        if not self.database_exists():
            raise DdeError("td_database_must_already_exist:" + self.database)
        ddl = (f"CREATE STABLE IF NOT EXISTS {self.database}.{SUPER_TABLE} "
               "(ts TIMESTAMP, ddje DOUBLE, version VARCHAR(24)) TAGS (symbol VARCHAR(8))")
        self._run(ddl)
        return ddl

    def latest_watermarks(self) -> dict[str, str]:
        rows = self._run(f"SELECT symbol, LAST(ts) FROM {self.database}.{SUPER_TABLE} GROUP BY symbol")
        return {str(symbol): _date_text(value) for symbol, value in rows if _date_text(value)}

    def read_target_rows(self, symbols: Sequence[str], target_date: str) -> dict[str, dict[str, Any]]:
        """Read the target-day projection from TD in bounded chunks."""
        stamp = _ts_us(target_date)
        found: dict[str, dict[str, Any]] = {}
        unique = sorted(set(str(symbol) for symbol in symbols))
        for offset in range(0, len(unique), 500):
            chunk = unique[offset:offset + 500]
            if not chunk:
                continue
            quoted = ",".join("'" + symbol + "'" for symbol in chunk if _SYMBOL_RE.fullmatch(symbol))
            if not quoted:
                continue
            rows = self._run(f"SELECT symbol, ts, ddje FROM {self.database}.{SUPER_TABLE} "
                             f"WHERE ts={stamp} AND symbol IN ({quoted})")
            for symbol, ts, ddje in rows:
                day = _date_text(ts)
                code = str(symbol)
                if day == target_date and code in chunk:
                    found[code] = {"symbol": code, "date": day, "ddje": _number(ddje)}
        return found

    def read_suspended(self, symbols: Sequence[str], target_date: str) -> set[str]:
        """Symbols whose daily_kline row of ``target_date`` says suspended: Kaipan has no same-day DDE for them."""
        stamp = _ts_us(target_date)
        unique = sorted(set(str(symbol) for symbol in symbols if _SYMBOL_RE.fullmatch(str(symbol))))
        found: set[str] = set()
        for offset in range(0, len(unique), 500):
            quoted = ",".join("'" + symbol + "'" for symbol in unique[offset:offset + 500])
            rows = self._run(f"SELECT symbol FROM {self.database}.daily_kline_v1 "
                             f"WHERE ts={stamp} AND suspended=true AND symbol IN ({quoted})")
            found.update(str(row[0]) for row in rows)
        return found

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
            f"SELECT ts, ddje, version FROM {self.database}.{child_table} "
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
        legacy = self._run("SELECT symbol, ts, ddje FROM market_data1.daily_dde "
                           f"WHERE ts >= '{first} 00:00:00' AND ts < '{after_last} 00:00:00' "
                           f"AND symbol IN ({quoted})")
        old = {}
        for item in legacy:
            day = _date_text(item[1])
            if day:
                old[(str(item[0]), day)] = {"ddje": item[2]}
        columns = {"ddje": {"equal": 0, "different": 0, "missing_legacy": 0}}
        reasons: dict[str, int] = {}
        equal = different = unmatched = 0
        for row in rows:
            prior = old.get((str(row["symbol"]), row["date"]))
            if prior is None:
                unmatched += 1
                reasons["no_legacy_row"] = reasons.get("no_legacy_row", 0) + 1
                continue
            row_equal = True
            name = "ddje"
            want, have = row.get(name), prior.get(name)
            if want is None and have is None:
                columns[name]["equal"] += 1
            elif want is None:
                columns[name]["different"] += 1
                row_equal = False
                reason = "source_ddje_missing_legacy_zero" if _number(have) == 0 else "source_ddje_missing"
                reasons[reason] = reasons.get(reason, 0) + 1
            elif have is None:
                columns[name]["missing_legacy"] += 1
                row_equal = False
                reasons["legacy_ddje_missing"] = reasons.get("legacy_ddje_missing", 0) + 1
            elif math.isclose(float(want), float(have), rel_tol=1e-6, abs_tol=1e-6):
                columns[name]["equal"] += 1
            else:
                columns[name]["different"] += 1
                row_equal = False
                reasons["numeric_difference:ddje"] = reasons.get("numeric_difference:ddje", 0) + 1
            if row_equal:
                equal += 1
            else:
                different += 1
        return {"pairs": len(rows), "equal": equal, "different": different, "unmatched": unmatched,
                "columns": columns, "reasons": reasons}

    @staticmethod
    def _release(cursor) -> None:
        """Free the statement's result handle now; TD keeps a fetched query registered until its cursor closes."""
        try:
            cursor.close()
        except Exception:
            pass

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


def _is_confirmed_empty_response(raw_response: Any) -> bool:
    if not isinstance(raw_response, Mapping):
        return False
    if str(raw_response.get("errcode", "0")) not in {"", "0", "None"}:
        return False
    dates = raw_response.get("Date")
    return isinstance(dates, (list, tuple)) and not dates


def _fetch_with_retry(fetcher: Callable[[str, str], Any], symbol: str, target_date: str,
                      history_trading_days: int, attempts: int,
                      sleep: Callable[[float], None], known_symbols: set[str],
                      before_request: Callable[[], None]
                      ) -> tuple[Partition | None, str | None, int, bool]:
    last_error = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            before_request()
            raw = fetcher(symbol, target_date)
            if _is_confirmed_empty_response(raw):
                if symbol in known_symbols:
                    return None, None, attempt, True
                raise DdeError("kaipan_dde_empty_for_unknown_symbol")
            return build_daily_dde(raw, symbol=symbol, target_date=target_date,
                                   history_trading_days=history_trading_days), None, attempt, False
        except Exception as exc:
            last_error = type(exc).__name__ + ":" + str(exc)[:180]
            if attempt < attempts:
                sleep(min(30.0, 0.5 * 2 ** (attempt - 1)))
    return None, last_error, attempts, False


def _redis_hot_view_config() -> tuple[str, int]:
    registry_path = Path(__file__).parents[1] / "guardian" / "datasets.registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    dataset = next((item for item in registry.get("items", []) if item.get("id") == DATASET), None)
    view = dataset.get("redis_hot_view") if isinstance(dataset, Mapping) else None
    if not isinstance(view, Mapping) or not view.get("namespace"):
        raise DdeError("dde_redis_hot_view_not_registered")
    ttl = int(view.get("ttl_seconds") or 0)
    if ttl < 1:
        raise DdeError("dde_redis_ttl_not_registered")
    return str(view["namespace"]).rstrip(":"), ttl


def _configure_kaipan_request_timeout(client: Any) -> None:
    """Bound the Kaipan SDK's requests.post call, which otherwise has no timeout."""
    requests_module = getattr(client, "requests", None)
    original_post = getattr(requests_module, "post", None)
    if not callable(original_post):
        raise DdeError("kaipan_requests_post_unavailable")
    if getattr(original_post, "__engine_core_timeout_wrapper__", False):
        return

    def post_with_timeout(*args, **kwargs):
        kwargs["timeout"] = KAIPAN_REQUEST_TIMEOUT
        return original_post(*args, **kwargs)

    post_with_timeout.__engine_core_timeout_wrapper__ = True
    requests_module.post = post_with_timeout


def run_daily_dde_sync(symbols: Sequence[str] | None, target_date: str, *, database: str | None = None,
                       root: str | Path | None = None, max_workers: int = DEFAULT_MAX_WORKERS, attempts: int = 3,
                       history_trading_days: int = DEFAULT_HISTORY_TRADING_DAYS,
                       redis_ttl_seconds: int | None = None, write_redis: bool = True,
                       skip_redis: bool = False, store: TaosDdeStore | None = None,
                       fetcher: Callable[[str, str], Any] | None = None,
                       sleep: Callable[[float], None] = time.sleep,
                       clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    target = _date_text(target_date)
    if target is None:
        raise ValueError("invalid_dde_target_date")
    if isinstance(max_workers, bool) or not 1 <= int(max_workers) <= 4:
        raise ValueError("dde_max_workers_must_be_1_to_4")
    if isinstance(attempts, bool) or not 1 <= int(attempts) <= 5:
        raise ValueError("dde_attempts_must_be_1_to_5")
    if isinstance(history_trading_days, bool) or not 1 <= int(history_trading_days) <= 5000:
        raise ValueError("history_trading_days_must_be_1_to_5000")
    started_at = clock()
    requested_workers = int(max_workers)
    max_workers = 1
    last_request_at: float | None = None

    def before_request() -> None:
        nonlocal last_request_at
        if last_request_at is not None:
            delay = 1.0 - (clock() - last_request_at)
            if delay > 0:
                sleep(delay)
        last_request_at = clock()
    redis_namespace, registry_ttl = _redis_hot_view_config()
    redis_ttl_seconds = registry_ttl if redis_ttl_seconds is None else int(redis_ttl_seconds)
    if redis_ttl_seconds < 1:
        raise ValueError("dde_redis_ttl_must_be_positive")
    base = Path(root or os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
    database_name = database or os.environ.get("CORE_TD_DATABASE") or "core_data"
    own = store is None
    store = store or TaosDdeStore(database_name)
    if fetcher is None:
        import importlib
        pykaipan = importlib.import_module("pykaipan.pykaipan")      # loaded by name: engine_core imports no SDK statically
        _configure_kaipan_request_timeout(pykaipan)

        def fetch_from_kaipan(symbol: str, day: str):
            size = INCREMENTAL_FETCH_SIZE if symbol in before else int(history_trading_days)
            return pykaipan.getHisStockDDE(symbol, size=str(size))

        fetcher = fetch_from_kaipan
    try:
        ddl = store.ensure_schema()
        before = store.latest_watermarks()
        candidates = sorted(set(str(item).strip().zfill(6) for item in symbols)) if symbols is not None else _active_symbols(base, target)
        if any(not _SYMBOL_RE.fullmatch(item) for item in candidates):
            raise ValueError("invalid_dde_symbol_list")
        known_symbols = set(candidates) if symbols is None else set()
        if symbols is not None:
            try:
                known_symbols = set(_active_symbols(base, target))
            except Exception:
                # Explicit-symbol callers may use an isolated root without a catalog.
                known_symbols = set()
        requested = [symbol for symbol in candidates if before.get(symbol, "") < target]
        skipped_completed = len(candidates) - len(requested)
        failed: dict[str, str] = {}
        known_missing: dict[str, str] = {}
        request_count = 0
        written_rows = 0
        written_symbols = 0
        comparison = {"pairs": 0, "equal": 0, "different": 0, "unmatched": 0,
                      "columns": {}, "reasons": {}}
        comparison_symbols: list[str] = []
        comparison_rows: list[dict[str, Any]] = []

        def flush_comparison() -> None:
            nonlocal comparison_symbols, comparison_rows
            if not comparison_rows:
                return
            part = store.compare_legacy(comparison_symbols, comparison_rows)
            for key in ("pairs", "equal", "different", "unmatched"):
                comparison[key] += int(part.get(key) or 0)
            for column, values in (part.get("columns") or {}).items():
                target_values = comparison["columns"].setdefault(column, {})
                for key, value in values.items():
                    target_values[key] = target_values.get(key, 0) + int(value or 0)
            for key, value in (part.get("reasons") or {}).items():
                comparison["reasons"][key] = comparison["reasons"].get(key, 0) + int(value or 0)
            comparison_symbols, comparison_rows = [], []

        after = dict(before)
        processed = 0
        last_progress_at = time.monotonic()
        print(f"DDE_START target={target} candidates={len(candidates)} to_fetch={len(requested)} "
              f"skipped_completed={skipped_completed} workers={int(max_workers)} attempts={int(attempts)}",
              flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_workers)) as pool:
            iterator = iter(requested)
            pending: dict[concurrent.futures.Future, str] = {}
            for _ in range(min(int(max_workers), len(requested))):
                symbol = next(iterator, None)
                if symbol is not None:
                    pending[pool.submit(_fetch_with_retry, fetcher, symbol, target,
                                        int(history_trading_days), int(attempts), sleep,
                                        known_symbols, before_request)] = symbol
            while pending:
                done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    symbol = pending.pop(future)
                    try:
                        partition, error, used, source_empty = future.result()
                    except Exception as exc:
                        partition, error, used, source_empty = None, type(exc).__name__ + ":" + str(exc)[:180], 1, False
                    request_count += used
                    if source_empty:
                        known_missing[symbol] = "kaipan_confirmed_empty_response"
                    elif partition is None:
                        failed[symbol] = error or "fetch_failed"
                    else:
                        try:
                            count = store.write_and_verify(symbol, partition.rows, partition.manifest["version"])
                            if count != len(partition.rows):
                                raise DdeError("td_readback_row_count_mismatch")
                            written_symbols += 1
                            written_rows += count
                            latest = max(row["date"] for row in partition.rows)
                            after[symbol] = max(after.get(symbol, ""), latest)
                            comparison_symbols.append(symbol)
                            comparison_rows.extend(partition.rows)
                            if len(comparison_symbols) >= 50:
                                flush_comparison()
                        except Exception as exc:
                            failed[symbol] = type(exc).__name__ + ":" + str(exc)[:180]
                    processed += 1
                    remaining = len(requested) - processed
                    now = time.monotonic()
                    if processed % 25 == 0 or now - last_progress_at >= 60 or remaining == 0:
                        print(f"DDE_PROGRESS target={target} written={written_symbols} td_rows={written_rows} "
                              f"failed={len(failed)} known_missing={len(known_missing)} "
                              f"remaining={remaining} skipped_completed={skipped_completed} "
                              f"requests={request_count}", flush=True)
                        last_progress_at = now
                    next_symbol = next(iterator, None)
                    if next_symbol is not None:
                        pending[pool.submit(_fetch_with_retry, fetcher, next_symbol, target,
                                            int(history_trading_days), int(attempts), sleep,
                                            known_symbols, before_request)] = next_symbol
        flush_comparison()

        stale = [symbol for symbol, reason in failed.items() if reason.startswith("DdeStaleResponse:")]
        if stale:
            # Kaipan publishes no same-day DDE for a suspended stock: that is the source's answer, not a fault.
            for symbol in sorted(store.read_suspended(stale, target)):
                known_missing[symbol] = "suspended_no_same_day_dde"
                del failed[symbol]

        # The run persists each verified symbol immediately. Any remaining source or
        # TD error is retryable; Guardian can resume from the single aggregate watermark query.
        summary = {"database": store.database, "target_date": target,
                   "candidate_symbols": len(candidates), "requested_symbols": len(requested),
                   "skipped_completed_symbols": skipped_completed,
                   "request_count": request_count, "history_trading_days": int(history_trading_days),
                   "max_workers": int(max_workers), "requested_max_workers": requested_workers, "written_symbols": written_symbols,
                   "rows_written": written_rows, "known_missing": [
                       {"symbol": symbol, "reason": reason} for symbol, reason in sorted(known_missing.items())],
                   "failed": [{"symbol": symbol, "reason": reason} for symbol, reason in sorted(failed.items())],
                   "readback_verified": not failed,
                   "watermarks_before": {symbol: before.get(symbol) for symbol in candidates[:20]},
                   "watermarks_after": {symbol: after.get(symbol) for symbol in candidates[:20]},
                   "watermark_counts": {"before": len(before), "after": len(after)},
                   "legacy_comparison": comparison,
                   "elapsed_seconds": round(clock() - started_at, 3)}
        if failed:
            summary["status"] = "failed"
            summary["error_code"] = "DDE_SYNC_INCOMPLETE_RETRYABLE"
            print(f"DDE_FAILED target={target} written={written_symbols} failed={len(failed)} "
                  f"known_missing={len(known_missing)} remaining={len(failed)} "
                  f"error_code=DDE_SYNC_INCOMPLETE_RETRYABLE", flush=True)
            raise DdeSyncRetryableError(summary)

        completed_symbols = set(candidates) - set(known_missing)
        target_rows_by_symbol = store.read_target_rows(sorted(completed_symbols), target)
        missing_target_rows = sorted(completed_symbols - set(target_rows_by_symbol))
        if missing_target_rows:
            summary["failed"] = [{"symbol": symbol, "reason": "td_target_date_row_missing"}
                                 for symbol in missing_target_rows]
            summary["status"] = "failed"
            summary["error_code"] = "DDE_SYNC_INCOMPLETE_RETRYABLE"
            print(f"DDE_FAILED target={target} written={written_symbols} failed={len(missing_target_rows)} "
                  f"known_missing={len(known_missing)} remaining={len(missing_target_rows)} "
                  f"error_code=DDE_SYNC_INCOMPLETE_RETRYABLE", flush=True)
            raise DdeSyncRetryableError(summary)

        redis_result: dict[str, Any] = {"written": False, "readback_verified": False, "key": None, "ttl": None}
        if skip_redis:
            redis_result["skipped"] = True
        elif write_redis and target_rows_by_symbol:
            redis_url = os.environ.get("REDIS_URL") or os.environ.get("CORE_REDIS_URL")
            if not redis_url:
                raise DdeError("REDIS_URL_required_for_dde_view")
            import redis
            key = redis_namespace + ":" + target
            target_rows = {symbol: json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                           for symbol, row in target_rows_by_symbol.items()}
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
            if target_rows:
                client.hset(key, mapping=target_rows)
                client.expire(key, int(redis_ttl_seconds))
            readback = client.hgetall(key)
            ttl = int(client.ttl(key))
            fields_verified = all(readback.get(symbol) == payload for symbol, payload in target_rows.items())
            redis_result = {"written": bool(target_rows), "readback_verified": fields_verified,
                            "key": key, "type": client.type(key), "ttl": ttl,
                            "row_count": len(target_rows)}
            if not fields_verified or ttl < 1:
                raise DdeError("dde_redis_readback_failed")
        summary.update({"status": "ok", "ddl": ddl, "readback_mismatches": 0,
                       "redis_readback_verified": bool(redis_result.get("readback_verified")),
                       "redis": redis_result})
        summary.pop("failed", None)
        print(f"DDE_COMPLETE target={target} symbols={len(candidates)} written={written_symbols} "
              f"skipped_completed={skipped_completed} td_rows={written_rows} "
              f"known_missing={json.dumps(summary['known_missing'], ensure_ascii=False, separators=(',', ':'))} "
              f"elapsed_seconds={summary['elapsed_seconds']}", flush=True)
        return summary
    finally:
        if own:
            store.close()
