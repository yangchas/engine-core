"""Baostock daily-bar adapter. Network clients and the symbol universe are injected."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import queue
import threading
import time
from concurrent.futures import TimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ...daily_kline_capture import parse_row, to_baostock_code


class _BaostockCallTimeout(TimeoutError):
    """Caller deadline expired while the daemon worker may still be blocked."""


class _BaostockStall(_BaostockCallTimeout):
    """No new row or checkpoint arrived during the configured progress window."""

    def __init__(self) -> None:
        super().__init__("STALL_NO_PROGRESS")
        self.error_code = "STALL_NO_PROGRESS"


class BaostockDailyProvider:
    source = "baostock"
    source_dependency = "independent_provider"
    available_at_rule = "trade_date 17:30 Asia/Shanghai"

    def __init__(self, *, client_factory: Callable[[], Any], symbols: Sequence[str] | Callable[[str], Sequence[str]],
                 timeout_seconds: float = 30, retries: int = 2, max_consecutive_errors: int = 25,
                 sleep_fn: Callable[[float], None] = time.sleep, timeout_call: Callable[..., Any] | None = None,
                 checkpoint_path: str | Path | None = None,
                 source_universe_checkpoint: str | Path | None = None,
                 total_timeout_seconds: float | None = None, sample_scope: bool = False,
                 no_progress_timeout_seconds: float = 300):
        self.client_factory = client_factory
        self.symbols = symbols
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        if total_timeout_seconds is not None and total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds_must_be_positive")
        if no_progress_timeout_seconds <= 0:
            raise ValueError("no_progress_timeout_seconds_must_be_positive")
        self.total_timeout_seconds = total_timeout_seconds
        self.no_progress_timeout_seconds = float(no_progress_timeout_seconds)
        self.deadline: float | None = None
        self.progress_deadline: float | None = None
        self.retries = max(0, int(retries))
        self.max_consecutive_errors = max(1, int(max_consecutive_errors))
        self.sleep_fn = sleep_fn
        self.timeout_call = timeout_call or self._timeout_call
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self.source_universe_checkpoint = (Path(source_universe_checkpoint)
                                           if source_universe_checkpoint is not None else None)
        self.sample_scope = bool(sample_scope)
        self._timing: dict[str, Any] = {"login_ms": 0.0, "query_ms": 0.0,
                                        "query_count": 0, "error_code": None, "error_events": []}
        self._checkpoint_saved = False

    CHECKPOINT_EVERY = 25

    def _load_checkpoint(self, trade_date: str, expected: Sequence[str]) -> dict[str, dict[str, Any]]:
        if self.checkpoint_path is None or not self.checkpoint_path.is_file():
            return {}
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if payload.get("trade_date") != trade_date or not isinstance(payload.get("rows"), list):
            return {}
        expected_digest = hashlib.sha256("\n".join(expected).encode("ascii")).hexdigest()
        stored_digest = payload.get("symbols_sha256")
        if ((self.sample_scope and stored_digest != expected_digest) or
                (stored_digest is not None and stored_digest != expected_digest)):
            return {}
        return {str(row["symbol"]): row for row in payload["rows"]
                if isinstance(row, dict) and row.get("symbol") and str(row["symbol"]) in expected}

    def _save_checkpoint(self, trade_date: str, rows: Mapping[str, Mapping[str, Any]],
                         expected: Sequence[str]) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        symbols_digest = hashlib.sha256("\n".join(expected).encode("ascii")).hexdigest()
        temporary.write_text(json.dumps({"trade_date": trade_date, "symbols_sha256": symbols_digest,
            "rows": [dict(rows[key]) for key in sorted(rows)]}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.checkpoint_path)
        self._checkpoint_saved = True

    def _timeout_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, fn(*args)))
            except BaseException as exc:
                result.put((False, exc))

        timeout = self.timeout_seconds
        stall_deadline = self.progress_deadline
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("BAOSTOCK_TIMEOUT") from None
        if stall_deadline is not None:
            timeout = min(timeout, stall_deadline - time.monotonic())
            if timeout <= 0:
                raise _BaostockStall() from None
        worker = threading.Thread(target=invoke, name="baostock-call", daemon=True)
        worker.start()
        try:
            succeeded, value = result.get(timeout=timeout)
        except queue.Empty:
            if (stall_deadline is not None and time.monotonic() >= stall_deadline and
                    (self.deadline is None or stall_deadline <= self.deadline)):
                raise _BaostockStall() from None
            raise _BaostockCallTimeout("BAOSTOCK_TIMEOUT") from None
        if succeeded:
            return value
        if isinstance(value, TimeoutError):
            raise TimeoutError("BAOSTOCK_TIMEOUT") from None
        if isinstance(value, (ConnectionError, OSError)):
            raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from value
        raise value

    LOGIN_TIMEOUT_FACTOR = 5.0  # login gets 5x the per-query bound (30s -> 150s)

    def _error_code(self, client: Any, exc: BaseException) -> str:
        if isinstance(exc, _BaostockStall):
            return "STALL_NO_PROGRESS"
        code = getattr(client, "last_error_code", None) or getattr(exc, "error_code", None)
        if code and str(code) != "0":
            return str(code)
        if isinstance(exc, TimeoutError) or "BAOSTOCK_TIMEOUT" in str(exc):
            return "BAOSTOCK_TIMEOUT"
        message = str(exc).lower()
        if self._is_connection_failure(exc, client):
            return "BAOSTOCK_CONNECTION_ERROR"
        if "login" in message and any(marker in message for marker in ("fail", "error", "denied")):
            return "BAOSTOCK_LOGIN_FAILED"
        return type(exc).__name__

    @staticmethod
    def _is_connection_failure(exc: BaseException, client: Any | None = None) -> bool:
        if isinstance(exc, (ConnectionError, OSError)):
            return True
        message = str(exc).lower()
        markers = (
            "baostock_connection_error", "bad file descriptor", "接收数据异常",
            "receive data error", "connection reset", "connection aborted",
            "broken pipe", "not connected", "socket is closed", "socket closed",
            "login failed", "login error", "login denied",
        )
        if any(marker in message for marker in markers):
            return True
        code = getattr(client, "last_error_code", None) if client is not None else None
        return code is not None and str(code) not in {"", "0"}

    def _annotate_exception(self, exc: BaseException, client: Any) -> None:
        self._timing["result"] = ("timeout" if "TIMEOUT" in str(exc).upper()
                                   or isinstance(exc, TimeoutError) else "error")
        self._timing.setdefault("started_at", datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
        if self._timing.get("error_code") in (None, "0"):
            self._timing["error_code"] = self._error_code(client, exc)
        try:
            exc.baostock_timing = dict(self._timing)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass

    def _record_error(self, stage: str, client: Any, exc: BaseException) -> dict[str, Any]:
        event = {"stage": stage, "error_type": type(exc).__name__,
                 "error_code": self._error_code(client, exc), "message": str(exc),
                 "retryable": self._is_timeout(exc) or self._is_connection_failure(exc, client)}
        self._timing.setdefault("error_events", []).append(event)
        return event

    def _timed_login_call(self, client: Any) -> None:
        started = time.monotonic()
        try:
            self.timeout_call(client.login)
            self._timing["error_code"] = "0"
        except Exception as exc:
            self._timing["error_code"] = self._error_code(client, exc)
            self._record_error("login", client, exc)
            raise
        finally:
            self._timing["login_ms"] += round((time.monotonic() - started) * 1000, 3)

    def _login(self, client: Any) -> None:
        """Baostock login latency spikes to ~50s under load (seen 2026-10-21 evening vs 0.4s normally): give it its own,
        longer bound and retry it, instead of abandoning the whole pull on the per-query 30s limit."""
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            if not self._deadline_left():
                raise TimeoutError("BAOSTOCK_TIMEOUT") from None
            saved = self.timeout_seconds
            try:
                self.timeout_seconds = saved * self.LOGIN_TIMEOUT_FACTOR if self.timeout_call == self._timeout_call else saved
                self._timed_login_call(client)
                return
            except _BaostockCallTimeout as exc:
                if self._timing.get("error_events"):
                    self._timing["error_events"][-1]["action"] = "call_timeout_abort"
                raise exc
            except (TimeoutError, RuntimeError, ConnectionError, OSError) as exc:
                last = exc
                if self._timing.get("error_events"):
                    self._timing["error_events"][-1]["action"] = (
                        "login_retry" if attempt < self.retries else "retry_limit_reached")
                if attempt < self.retries:
                    delay = min(2 ** (attempt + 2), 30)
                    if self.deadline is not None:
                        remaining = max(0.0, self.deadline - time.monotonic())
                        if remaining <= 0:
                            raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                        delay = min(delay, remaining)
                    self.sleep_fn(delay)
            finally:
                self.timeout_seconds = saved
        error = last if last is not None else RuntimeError("BAOSTOCK_LOGIN_FAILED")
        self._annotate_exception(error, client)
        raise error

    def _deadline_left(self) -> bool:
        return self.deadline is None or self.deadline - time.monotonic() > 0

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        return isinstance(exc, TimeoutError) or "BAOSTOCK_TIMEOUT" in str(exc).upper()

    def _progress(self, trade_date: str, expected: Sequence[str], checkpoint: Mapping[str, Any],
                  resumed_count: int, result: str) -> dict[str, Any]:
        return {
            "completed_symbols": sum(symbol in checkpoint for symbol in expected),
            "expected_symbols": len(expected),
            "resumed_count": resumed_count,
            "checkpoint_saved": bool(self._checkpoint_saved or
                                      (self.checkpoint_path is not None and self.checkpoint_path.is_file())),
            "checkpoint_path": str(self.checkpoint_path) if self.checkpoint_path is not None else None,
            "result": result,
        }

    def _query(self, client: Any, code: str, day: str) -> Any:
        query = client.query
        try:
            supports_timeout = "timeout_seconds" in inspect.signature(query).parameters
        except (TypeError, ValueError):
            supports_timeout = False
        args = (code, day, self.timeout_seconds) if supports_timeout else (code, day)
        self._timing["query_count"] = int(self._timing.get("query_count", 0)) + 1
        started = time.monotonic()
        try:
            result = self.timeout_call(query, *args)
            self._timing["error_code"] = "0"
            return result
        except Exception as exc:
            self._timing["error_code"] = self._error_code(client, exc)
            self._record_error("query", client, exc)
            raise
        finally:
            self._timing["query_ms"] += round((time.monotonic() - started) * 1000, 3)

    def _source_universe(self, client: Any, day: str) -> tuple[list[str] | None, str | None]:
        path = self.source_universe_checkpoint
        if path is not None and path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                symbols = payload.get("symbols") if payload.get("trade_date") == day else None
                if isinstance(symbols, list) and symbols:
                    return sorted({str(value).zfill(6) for value in symbols}), None
            except (OSError, ValueError, TypeError):
                pass
        module = getattr(client, "module", None)
        query = getattr(module, "query_all_stock", None)
        if not callable(query):
            return None, "query_all_stock_unavailable"
        started = time.monotonic()
        try:
            self._timing["query_count"] = int(self._timing.get("query_count", 0)) + 1
            result = self.timeout_call(lambda: query(day=day))
            if str(getattr(result, "error_code", "0")) != "0":
                return None, "query_all_stock_error"
            fields = list(getattr(result, "fields", ()) or ())
            if "code" not in fields:
                return None, "query_all_stock_schema_invalid"
            code_index = fields.index("code")
            symbols: set[str] = set()
            while result.next():
                row = result.get_row_data()
                if code_index >= len(row):
                    continue
                symbol = str(row[code_index]).rsplit(".", 1)[-1]
                if symbol.isdigit() and len(symbol) == 6:
                    symbols.add(symbol)
            if not symbols:
                return None, "query_all_stock_empty"
            values = sorted(symbols)
            if path is not None:
                from ...data_pipeline.storage import _atomic_write, canonical_json
                payload = {"trade_date": day, "source": "baostock.query_all_stock",
                           "symbols": values}
                _atomic_write(path, canonical_json(payload) + b"\n")
            return values, None
        except Exception as exc:
            return None, "query_all_stock_failed:" + type(exc).__name__
        finally:
            self._timing["query_ms"] += round((time.monotonic() - started) * 1000, 3)

    def fetch(self, trade_date: str, context: Any = None) -> dict[str, Any]:
        self._timing = {"login_ms": 0.0, "query_ms": 0.0, "query_count": 0,
                        "error_code": None, "error_events": [],
                        "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
        self._checkpoint_saved = False
        self.progress_deadline = None
        self.deadline = (time.monotonic() + self.total_timeout_seconds
                         if self.total_timeout_seconds is not None else None)
        if context is not None:
            context.request()
        universe = self.symbols(trade_date) if callable(self.symbols) else self.symbols
        expected = list(dict.fromkeys(str(value).zfill(6) for value in universe))
        client = self.client_factory()
        if hasattr(client, "timeout_seconds"):
            client.timeout_seconds = self.timeout_seconds
        checkpoint = self._load_checkpoint(trade_date, expected)
        resumed_count = sum(symbol in checkpoint for symbol in expected)
        rows: list[dict[str, Any]] = [row for row in checkpoint.values()
                                      if not row.get("__checkpoint_no_bar")]
        missing: list[str] = [symbol for symbol, row in checkpoint.items()
                              if row.get("__checkpoint_no_bar")]
        errors: list[dict[str, str]] = []
        consecutive = 0
        transport_failed = False
        unsaved = 0
        budget_exhausted = False
        source_symbols: list[str] | None = None
        source_universe_error: str | None = "not_queried"
        try:
            try:
                self._login(client)
                self.progress_deadline = time.monotonic() + self.no_progress_timeout_seconds
            except Exception as exc:
                if isinstance(exc, _BaostockCallTimeout):
                    transport_failed = True
                if self.deadline is not None and not self._deadline_left() and self._is_timeout(exc):
                    budget_exhausted = True
                else:
                    raise
            if self.sample_scope:
                source_universe_error = "sample_scope"
            else:
                source_symbols, source_universe_error = self._source_universe(client, trade_date)
            for symbol in expected:
                if budget_exhausted:
                    break
                if symbol in checkpoint:
                    continue
                if not self._deadline_left():
                    budget_exhausted = True
                    break
                code = to_baostock_code(symbol)
                response = None
                last_error: Exception | None = None
                for attempt in range(self.retries + 1):
                    if not self._deadline_left():
                        budget_exhausted = True
                        break
                    try:
                        response = self._query(client, code, trade_date)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        timed_out = self._is_timeout(exc)
                        transport = timed_out or self._is_connection_failure(exc, client)
                        if transport:
                            event = self._timing.get("error_events", [{}])[-1]
                            if isinstance(exc, _BaostockStall):
                                event["action"] = "stall_no_progress"
                                transport_failed = True
                                self._annotate_exception(exc, client)
                                raise exc
                            if isinstance(exc, _BaostockCallTimeout):
                                # The daemon worker may still own the shared SDK socket. Do not
                                # logout/reuse it in this process; return retryably at the caller.
                                event["action"] = "call_timeout_abort"
                                transport_failed = True
                                if self.deadline is not None and not self._deadline_left():
                                    event["action"] = "total_budget_exhausted_checkpoint"
                                    budget_exhausted = True
                                    break
                                raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                            if timed_out:
                                self._annotate_exception(exc, client)
                                if self.deadline is not None and not self._deadline_left():
                                    event["action"] = "total_budget_exhausted_checkpoint"
                                    transport_failed = True
                                    budget_exhausted = True
                                    break
                            if attempt < self.retries and self._deadline_left():
                                event["action"] = "bounded_logout_fresh_login_retry"
                                delay = min(2 ** (attempt + 2), 30)
                                if self.deadline is not None:
                                    delay = min(delay, max(0.0, self.deadline - time.monotonic()))
                                if delay <= 0:
                                    budget_exhausted = True
                                    break
                                self.sleep_fn(delay)
                                if not self._deadline_left():
                                    budget_exhausted = True
                                    transport_failed = True
                                    break
                                try:
                                    # Logout is itself bounded and can reset Baostock's shared
                                    # socket context before the fresh client's login.
                                    self.timeout_call(client.logout)
                                except Exception as logout_error:
                                    logout_event = self._record_error("logout", client, logout_error)
                                    if isinstance(logout_error, _BaostockCallTimeout):
                                        logout_event["action"] = "call_timeout_abort"
                                        transport_failed = True
                                        raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                                    logout_event["action"] = "cleanup_failed_continue"
                                if not self._deadline_left():
                                    budget_exhausted = True
                                    transport_failed = True
                                    break
                                try:
                                    client = self.client_factory()
                                    if hasattr(client, "timeout_seconds"):
                                        client.timeout_seconds = self.timeout_seconds
                                    self._login(client)
                                except Exception as relogin_error:
                                    self._annotate_exception(relogin_error, client)
                                    if self.deadline is not None and not self._deadline_left() and self._is_timeout(relogin_error):
                                        budget_exhausted = True
                                        break
                                    transport_failed = True
                                    if isinstance(relogin_error, _BaostockCallTimeout):
                                        raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                                    raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from relogin_error
                                continue
                            transport_failed = True
                            self._annotate_exception(exc, client)
                            event["action"] = "retry_limit_reached"
                            if timed_out:
                                error = TimeoutError("BAOSTOCK_TIMEOUT")
                                self._annotate_exception(error, client)
                                raise error from None
                            raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from exc
                        if attempt < self.retries:
                            if self._timing.get("error_events"):
                                self._timing["error_events"][-1]["action"] = "symbol_retry"
                            delay = min(2 ** attempt, 8)
                            if self.deadline is not None:
                                delay = min(delay, max(0.0, self.deadline - time.monotonic()))
                            if delay <= 0:
                                budget_exhausted = True
                                break
                            self.sleep_fn(delay)
                            if not self._deadline_left():
                                budget_exhausted = True
                                break
                if budget_exhausted:
                    break
                if last_error is not None:
                    errors.append({"symbol": symbol, "error_type": type(last_error).__name__})
                    if self._timing.get("error_events"):
                        self._timing["error_events"][-1]["action"] = "symbol_skipped"
                    consecutive += 1
                    if consecutive >= self.max_consecutive_errors:
                        break
                    continue
                found = list(response or [])
                if not found:
                    missing.append(symbol)
                    checkpoint[symbol] = {"symbol": symbol, "date": trade_date,
                                          "__checkpoint_no_bar": True}
                    self.progress_deadline = time.monotonic() + self.no_progress_timeout_seconds
                    unsaved += 1
                    if unsaved >= self.CHECKPOINT_EVERY:
                        self._save_checkpoint(trade_date, checkpoint, expected)
                        unsaved = 0
                    consecutive = 0
                    continue
                consecutive = 0
                for raw in found:
                    row = parse_row(raw)
                    if row["symbol"] != symbol or row["date"] != trade_date:
                        errors.append({"symbol": symbol, "error_type": "IdentityMismatch"})
                        continue
                    computed_pct = ((row["close"] / row["pre_close"] - 1) * 100
                                    if row.get("close") is not None and row.get("pre_close") else None)
                    source_pct = row.get("pct_chg")
                    row["mismatch"] = bool(computed_pct is not None and source_pct is not None
                                           and abs(computed_pct - source_pct) > 0.02)
                    row["turnover"] = row.get("turnover_pct")
                    rows.append(row)
                    checkpoint[symbol] = row
                    self.progress_deadline = time.monotonic() + self.no_progress_timeout_seconds
                    unsaved += 1
                # Resume point: rewriting the whole checkpoint per symbol made the capture O(n^2) (~1.5 symbols/s instead
                # of the ~5/s Baostock answers at), so persist every CHECKPOINT_EVERY symbols and always on the way out.
                if unsaved >= self.CHECKPOINT_EVERY:
                    self._save_checkpoint(trade_date, checkpoint, expected)
                    unsaved = 0
        except Exception as exc:
            self._annotate_exception(exc, client)
            try:
                exc.baostock_progress = self._progress(trade_date, expected, checkpoint, resumed_count,
                                                       "timeout" if self._is_timeout(exc) else "error")
            except (AttributeError, TypeError):
                pass
            raise
        finally:
            if self.checkpoint_path is not None and (unsaved or budget_exhausted or
                                                     not self.checkpoint_path.is_file()):
                self._save_checkpoint(trade_date, checkpoint, expected)
            if not transport_failed and self._deadline_left():
                try:
                    self.timeout_call(client.logout)
                except Exception as exc:
                    event = self._record_error("logout", client, exc)
                    event["action"] = "cleanup_warning_data_retained"
        suspended = sum(bool(row.get("suspended")) for row in rows)
        mismatch = sum(bool(row.get("mismatch")) for row in rows)
        result = "budget_exhausted" if budget_exhausted else "error" if errors else "ok"
        if budget_exhausted:
            self._timing["result"] = "budget_exhausted"
            self._timing["error_code"] = "BAOSTOCK_TOTAL_TIMEOUT"
        progress = self._progress(trade_date, expected, checkpoint, resumed_count, result)
        returned_rows = [row for row in rows if not row.get("__checkpoint_no_bar")]
        result_meta = {"rows": returned_rows, "expected_count": len(expected), "missing_count": len(missing),
                "missing_symbols": sorted(set(missing)), "suspended_count": suspended,
                "mismatch_count": mismatch, "errors": errors,
                "source_symbols": source_symbols,
                "source_universe_error": source_universe_error,
                "partial": bool(missing or errors or len(rows) < len(expected)),
                "resumed_count": resumed_count,
                **progress,
                "baostock_timing": {**self._timing, **progress,
                                    "error_code": (self._timing.get("error_code") or "0")}}
        if self.sample_scope:
            result_meta.update({"sample_scope": "symbols_file", "sample_symbols": expected,
                                "tier": 0, "source": self.source})
        return result_meta

