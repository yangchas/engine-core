"""Rate-limited, bounded Kaipan protocol adapter; the client is always injected."""
from __future__ import annotations

import re
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


class ProviderCircuitOpen(RuntimeError):
    pass


def _code(value: Any) -> str | None:
    match = re.search(r"(\d{6})$", str(value or "").strip())
    return match.group(1) if match else None


def _pool_rows(response: Any) -> list[list[Any]]:
    if not isinstance(response, Mapping) or not isinstance(response.get("info"), (list, tuple)):
        raise ValueError("kaipan_unexpected_pool_shape")
    result = []
    for page in response["info"]:
        if isinstance(page, (list, tuple)):
            result.extend(row for row in page if isinstance(row, (list, tuple)) and len(row) >= 1)
    return result


def _list_rows(response: Any) -> list[Any]:
    if isinstance(response, Mapping):
        for key in ("list", "List", "data", "info"):
            value = response.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
        raise ValueError("kaipan_unexpected_list_shape")
    if isinstance(response, (list, tuple)):
        return list(response)
    raise ValueError("kaipan_unexpected_list_shape")


class KaipanProvider:
    def __init__(self, client: Any, *, plate_codes: Iterable[str] = (), symbols: Iterable[str] = (),
                 plate_classes: Mapping[str, str] | None = None, max_requests: int = 30,
                 min_interval_seconds: float = 10, sleep_fn: Callable[[float], None] = time.sleep,
                 monotonic_fn: Callable[[], float] = time.monotonic,
                 reasons_checkpoint_path: str | Path | None = None):
        self.client = client
        self.plate_codes = tuple(dict.fromkeys(str(code) for code in plate_codes))
        self.symbols = tuple(dict.fromkeys(str(symbol).zfill(6) for symbol in symbols))
        self.plate_classes = dict(plate_classes or {})
        self.max_requests = max(1, int(max_requests))
        self.min_interval_seconds = max(10.0, float(min_interval_seconds))
        self.sleep_fn, self.monotonic_fn = sleep_fn, monotonic_fn
        self._requests = 0
        self._last_request: float | None = None
        self._tripped = False
        self.reasons_checkpoint_path = (Path(reasons_checkpoint_path)
                                        if reasons_checkpoint_path is not None else None)

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self._tripped:
            raise ProviderCircuitOpen("kaipan_circuit_open")
        if self._requests >= self.max_requests:
            raise RuntimeError("kaipan_request_limit")
        if self._last_request is not None:
            wait = self.min_interval_seconds - (self.monotonic_fn() - self._last_request)
            if wait > 0:
                self.sleep_fn(wait)
        self._requests += 1
        self._last_request = self.monotonic_fn()
        try:
            value = fn(*args)
        except Exception as exc:
            if any(token in str(exc).lower() for token in ("403", "forbidden", "captcha", "anti-bot")):
                self._tripped = True
                raise ProviderCircuitOpen("kaipan_provider_blocked") from None
            raise RuntimeError(f"kaipan_request_failed:{type(exc).__name__}") from None
        if isinstance(value, Mapping) and (value.get("status_code") == 403 or
                str(value.get("error", "")).lower() in {"403", "forbidden"}):
            self._tripped = True
            raise ProviderCircuitOpen("kaipan_provider_blocked")
        if value is None or value == [] or value == {}:
            self._tripped = True
            raise ProviderCircuitOpen("kaipan_empty_response")
        return value

    def limit_up_pool(self, trade_date: str, *, max_levels: int = 10,
                      context: Any = None) -> dict[str, Any]:
        rows, seen = [], set()
        for level in range(1, max_levels + 1):
            response = self._call(self.client.get_his_bans, trade_date, level)
            for raw in _pool_rows(response):
                symbol = _code(raw[0])
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                rows.append({"symbol": symbol, "name": str(raw[1]) if len(raw) > 1 else None,
                             "lb_days": level, "in_legacy_pool": True, "trade_date": trade_date})
        return {"rows": rows, "expected_count": len(seen), "request_count": self._requests}

    def plate_members(self, trade_date: str, *, context: Any = None,
                      page_size: int = 2000) -> dict[str, Any]:
        rows = []
        partial = False
        plates_completed = 0
        for plate in self.plate_codes:
            index = 0
            while True:
                if self._requests >= self.max_requests:
                    partial = True
                    break
                response = self._call(self.client.get_his_plate_ids, plate, trade_date, index, page_size)
                page = _list_rows(response)
                for raw in page:
                    if not isinstance(raw, (list, tuple)) or len(raw) < 1:
                        continue
                    symbol = _code(raw[0])
                    if symbol:
                        rows.append({"plate_code": plate, "symbol": symbol,
                                     "plate_class": self.plate_classes.get(plate, "UNKNOWN"),
                                     "plate_member_label": raw[4] if len(raw) > 4 else None,
                                     "trade_date": trade_date})
                count = response.get("Count") if isinstance(response, Mapping) else None
                index += len(page)
                if not page or (count is not None and index >= int(count)) or len(page) < page_size:
                    break
            if partial:
                break
            plates_completed += 1
        if not self.plate_codes:
            raise ValueError("kaipan_plate_codes_required")
        return {"rows": rows, "expected_count": len(rows), "partial": partial,
                "queried_plate_count": plates_completed,
                "unqueried_plate_count": max(0, len(self.plate_codes) - plates_completed),
                "request_count": self._requests}

    def hot_plates(self, trade_date: str, *, context: Any = None,
                   page_size: int = 50) -> dict[str, Any]:
        rows, index = [], 0
        while True:
            response = self._call(self.client.get_his_plates, trade_date, index, page_size)
            page = _list_rows(response)
            for raw in page:
                if isinstance(raw, Mapping):
                    row = dict(raw)
                    code = _code(row.get("plate_code", row.get("code")))
                    row["plate_code"] = code
                elif isinstance(raw, (list, tuple)) and raw:
                    row = {"plate_code": _code(raw[0]), "name": raw[1] if len(raw) > 1 else None,
                           "strength": raw[2] if len(raw) > 2 else None,
                           "change_pct": raw[3] if len(raw) > 3 else None,
                           "amount_yuan": raw[5] if len(raw) > 5 else None}
                else:
                    continue
                row["trade_date"] = trade_date
                rows.append(row)
            count = response.get("Count") if isinstance(response, Mapping) else None
            index += len(page)
            if not page or (count is not None and index >= int(count)) or len(page) < page_size:
                break
        return {"rows": rows, "expected_count": len(rows), "request_count": self._requests}

    def limit_reasons(self, trade_date: str, *, context: Any = None,
                      symbols: Iterable[str] | None = None) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        done: set[str] = set()
        if self.reasons_checkpoint_path is not None and self.reasons_checkpoint_path.is_file():
            try:
                checkpoint = json.loads(self.reasons_checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                checkpoint = {}
            if checkpoint.get("trade_date") == trade_date:
                done = set(map(str, checkpoint.get("done_symbols", ())))
                rows = [dict(row) for row in checkpoint.get("rows", ()) if isinstance(row, Mapping)]
        requested = tuple(symbols if symbols is not None else self.symbols)
        queried_now = 0
        for raw_symbol in requested:
            symbol = str(raw_symbol).zfill(6)
            if symbol in done:
                continue
            if self._requests >= self.max_requests:
                break
            response = self._call(self.client.get_ban_reasons, symbol)
            queried_now += 1
            done.add(symbol)
            if not isinstance(response, Mapping):
                raise ValueError("kaipan_unexpected_reason_shape")
            entries = response.get("List", response.get("list", []))
            if not isinstance(entries, (list, tuple)):
                raise ValueError("kaipan_unexpected_reason_shape")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                event_date = str(entry.get("Date") or "")[:10]
                if event_date and event_date > trade_date:
                    continue
                rows.append({"symbol": symbol, "event_date": event_date,
                             "type": str(entry.get("Type", "")), "reason": entry.get("Reason"),
                             "plate_codes": entry.get("ZSCode", []),
                             "parent_plate_code": entry.get("PZSCode"), "trade_date": trade_date})
            self._save_reasons_checkpoint(trade_date, done, rows)
        remaining = [str(symbol).zfill(6) for symbol in requested if str(symbol).zfill(6) not in done]
        return {"rows": rows, "expected_count": len(rows), "queried_symbol_count": queried_now,
                "unqueried_symbol_count": len(remaining), "partial": bool(remaining),
                "request_count": self._requests}

    def _save_reasons_checkpoint(self, trade_date: str, done: set[str],
                                 rows: Sequence[Mapping[str, Any]]) -> None:
        if self.reasons_checkpoint_path is None:
            return
        self.reasons_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.reasons_checkpoint_path.with_name(self.reasons_checkpoint_path.name + ".tmp")
        temporary.write_text(json.dumps({"trade_date": trade_date, "done_symbols": sorted(done),
            "rows": list(rows)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        os.replace(temporary, self.reasons_checkpoint_path)

