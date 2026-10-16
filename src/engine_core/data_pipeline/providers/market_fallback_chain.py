"""Rate-limited, fail-closed WenCai calls for explicitly requested fallbacks."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from .daily_kline_chain import _atomic_write, canonical_json


class WencaiMarketGate:
    """Shared policy for live market fallback calls: 15/min, 10s spacing, first-403 breaker."""

    def __init__(self, fetch: Callable[..., Any], *, state_path: str | Path,
                 max_requests: int = 15, min_interval_seconds: float = 10.0,
                 breaker_seconds: int = 3 * 60 * 60,
                 clock_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self.fetcher = fetch
        self.state_path = Path(state_path)
        self.max_requests = min(15, max(1, int(max_requests)))
        self.min_interval_seconds = max(10.0, float(min_interval_seconds))
        self.breaker_seconds = max(1, int(breaker_seconds))
        self.clock_fn, self.sleep_fn = clock_fn, sleep_fn
        self.last_failure: str | None = None

    def _state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def fetch(self, query: str, loop: bool = True, cap: int = 1000) -> Any:
        wall = self.clock_fn()
        state = self._state()
        if float(state.get("blocked_until", 0)) > wall:
            if self.last_failure != "provider_blocked":
                self.last_failure = "circuit_open"
            return {"status": "PROVIDER_UNAVAILABLE", "reason": "circuit_open"}
        history = [float(value) for value in state.get("requests", []) if wall - float(value) < 60]
        previous = float(state.get("last_request", 0))
        if previous:
            wait = self.min_interval_seconds - (wall - previous)
            if wait > 0:
                self.sleep_fn(wait)
                wall = self.clock_fn()
                history = [value for value in history if wall - value < 60]
        if len(history) >= self.max_requests:
            self.last_failure = "rate_limited"
            return {"status": "PROVIDER_UNAVAILABLE", "reason": "rate_limited"}
        history.append(wall)
        state.update({"requests": history, "last_request": wall})
        _atomic_write(self.state_path, canonical_json(state) + b"\n")
        try:
            result = self.fetcher(query, loop, cap)
        except Exception as exc:
            if re.search(r"403|forbidden", str(exc), re.I):
                self._open_breaker()
                self.last_failure = "provider_blocked"
                return {"status": "PROVIDER_UNAVAILABLE", "reason": "provider_blocked",
                        "status_code": 403}
            self.last_failure = f"fetch_failed:{type(exc).__name__}"
            return {"status": "PROVIDER_UNAVAILABLE", "reason": f"fetch_failed:{type(exc).__name__}"}
        if isinstance(result, dict) and (result.get("status_code") == 403 or
                re.search(r"403|forbidden", str(result.get("error", "")), re.I)):
            self._open_breaker()
            self.last_failure = "provider_blocked"
            return {"status": "PROVIDER_UNAVAILABLE", "reason": "provider_blocked",
                    "status_code": 403}
        self.last_failure = None
        return result

    def _open_breaker(self) -> None:
        state = self._state()
        state["blocked_until"] = self.clock_fn() + self.breaker_seconds
        _atomic_write(self.state_path, canonical_json(state) + b"\n")
