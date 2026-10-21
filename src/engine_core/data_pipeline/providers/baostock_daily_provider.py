"""Baostock daily-bar adapter. Network clients and the symbol universe are injected."""
from __future__ import annotations

import inspect
import json
import os
import queue
import threading
import time
from concurrent.futures import TimeoutError
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ...daily_kline_capture import parse_row, to_baostock_code


class BaostockDailyProvider:
    source = "baostock"
    source_dependency = "independent_provider"
    available_at_rule = "trade_date 17:30 Asia/Shanghai"

    def __init__(self, *, client_factory: Callable[[], Any], symbols: Sequence[str] | Callable[[str], Sequence[str]],
                 timeout_seconds: float = 30, retries: int = 2, max_consecutive_errors: int = 25,
                 sleep_fn: Callable[[float], None] = time.sleep, timeout_call: Callable[..., Any] | None = None,
                 checkpoint_path: str | Path | None = None,
                 total_timeout_seconds: float | None = None):
        self.client_factory = client_factory
        self.symbols = symbols
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        if total_timeout_seconds is not None and total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds_must_be_positive")
        self.total_timeout_seconds = total_timeout_seconds
        self.deadline: float | None = None
        self.retries = max(0, int(retries))
        self.max_consecutive_errors = max(1, int(max_consecutive_errors))
        self.sleep_fn = sleep_fn
        self.timeout_call = timeout_call or self._timeout_call
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None

    CHECKPOINT_EVERY = 25

    def _load_checkpoint(self, trade_date: str) -> dict[str, dict[str, Any]]:
        if self.checkpoint_path is None or not self.checkpoint_path.is_file():
            return {}
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if payload.get("trade_date") != trade_date or not isinstance(payload.get("rows"), list):
            return {}
        return {str(row["symbol"]): row for row in payload["rows"]
                if isinstance(row, dict) and row.get("symbol")}

    def _save_checkpoint(self, trade_date: str, rows: Mapping[str, Mapping[str, Any]]) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        temporary.write_text(json.dumps({"trade_date": trade_date,
            "rows": [dict(rows[key]) for key in sorted(rows)]}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.checkpoint_path)

    def _timeout_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, fn(*args)))
            except BaseException as exc:
                result.put((False, exc))

        timeout = self.timeout_seconds
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("BAOSTOCK_TIMEOUT") from None
        worker = threading.Thread(target=invoke, name="baostock-call", daemon=True)
        worker.start()
        try:
            succeeded, value = result.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("BAOSTOCK_TIMEOUT") from None
        if succeeded:
            return value
        if isinstance(value, TimeoutError):
            raise TimeoutError("BAOSTOCK_TIMEOUT") from None
        if isinstance(value, (ConnectionError, OSError)):
            raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from value
        raise value

    LOGIN_TIMEOUT_FACTOR = 5.0  # login gets 5x the per-query bound (30s -> 150s)

    def _login(self, client: Any) -> None:
        """Baostock login latency spikes to ~50s under load (seen 2026-10-21 evening vs 0.4s normally): give it its own,
        longer bound and retry it, instead of abandoning the whole pull on the per-query 30s limit."""
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            saved = self.timeout_seconds
            try:
                self.timeout_seconds = saved * self.LOGIN_TIMEOUT_FACTOR if self.timeout_call == self._timeout_call else saved
                self.timeout_call(client.login)
                return
            except (TimeoutError, RuntimeError) as exc:
                last = exc
                if attempt < self.retries:
                    self.sleep_fn(min(2 ** (attempt + 2), 30))
            finally:
                self.timeout_seconds = saved
        raise last if last is not None else RuntimeError("BAOSTOCK_LOGIN_FAILED")

    def _deadline_left(self) -> bool:
        return self.deadline is None or self.deadline - time.monotonic() > 0

    def _query(self, client: Any, code: str, day: str) -> Any:
        query = client.query
        try:
            supports_timeout = "timeout_seconds" in inspect.signature(query).parameters
        except (TypeError, ValueError):
            supports_timeout = False
        args = (code, day, self.timeout_seconds) if supports_timeout else (code, day)
        return self.timeout_call(query, *args)

    def fetch(self, trade_date: str, context: Any = None) -> dict[str, Any]:
        self.deadline = (time.monotonic() + self.total_timeout_seconds
                         if self.total_timeout_seconds is not None else None)
        if context is not None:
            context.request()
        universe = self.symbols(trade_date) if callable(self.symbols) else self.symbols
        expected = list(dict.fromkeys(str(value).zfill(6) for value in universe))
        client = self.client_factory()
        if hasattr(client, "timeout_seconds"):
            client.timeout_seconds = self.timeout_seconds
        checkpoint = self._load_checkpoint(trade_date)
        resumed_count = sum(symbol in checkpoint for symbol in expected)
        rows: list[dict[str, Any]] = list(checkpoint.values())
        missing: list[str] = []
        errors: list[dict[str, str]] = []
        consecutive = 0
        transport_failed = False
        unsaved = 0
        self._login(client)
        try:
            for symbol in expected:
                if symbol in checkpoint:
                    continue
                code = to_baostock_code(symbol)
                response = None
                last_error: Exception | None = None
                for attempt in range(self.retries + 1):
                    try:
                        response = self._query(client, code, trade_date)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        transport = (isinstance(exc, (TimeoutError, ConnectionError, OSError))
                                     or "BAOSTOCK_TIMEOUT" in str(exc) or "BAOSTOCK_CONNECTION_ERROR" in str(exc))
                        if transport:
                            # A stalled session (Baostock "接收数据异常" / hung call) is usually cured by a fresh login:
                            # back off, re-login and retry this symbol before giving the whole pull up.
                            if attempt < self.retries and self._deadline_left():
                                self.sleep_fn(min(2 ** (attempt + 2), 30))
                                try:
                                    self.timeout_call(client.logout)
                                except Exception:
                                    pass
                                try:
                                    self.timeout_call(client.login)
                                except Exception:
                                    pass
                                continue
                            transport_failed = True
                            if isinstance(exc, TimeoutError) or "BAOSTOCK_TIMEOUT" in str(exc):
                                raise TimeoutError("BAOSTOCK_TIMEOUT") from None
                            raise RuntimeError("BAOSTOCK_CONNECTION_ERROR") from exc
                        if attempt < self.retries:
                            self.sleep_fn(min(2 ** attempt, 8))
                if last_error is not None:
                    errors.append({"symbol": symbol, "error_type": type(last_error).__name__})
                    consecutive += 1
                    if consecutive >= self.max_consecutive_errors:
                        break
                    continue
                found = list(response or [])
                if not found:
                    missing.append(symbol)
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
                    unsaved += 1
                # Resume point: rewriting the whole checkpoint per symbol made the capture O(n^2) (~1.5 symbols/s instead
                # of the ~5/s Baostock answers at), so persist every CHECKPOINT_EVERY symbols and always on the way out.
                if unsaved >= self.CHECKPOINT_EVERY:
                    self._save_checkpoint(trade_date, checkpoint)
                    unsaved = 0
        finally:
            if unsaved:
                self._save_checkpoint(trade_date, checkpoint)
            if not transport_failed:
                self.timeout_call(client.logout)
        suspended = sum(bool(row.get("suspended")) for row in rows)
        mismatch = sum(bool(row.get("mismatch")) for row in rows)
        return {"rows": rows, "expected_count": len(expected), "missing_count": len(missing),
                "missing_symbols": missing, "suspended_count": suspended,
                "mismatch_count": mismatch, "errors": errors,
                "partial": bool(missing or errors or len(rows) < len(expected)),
                "resumed_count": resumed_count}

