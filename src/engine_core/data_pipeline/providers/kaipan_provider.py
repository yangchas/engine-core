"""Rate-limited, bounded Kaipan protocol adapter; the client is always injected."""
from __future__ import annotations

import re
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                 reasons_checkpoint_path: str | Path | None = None,
                 members_checkpoint_path: str | Path | None = None):
        self.client = client
        self.plate_codes = tuple(dict.fromkeys(str(code) for code in plate_codes))
        self.symbols = tuple(dict.fromkeys(str(symbol).zfill(6) for symbol in symbols))
        self.plate_classes = dict(plate_classes or {})
        self.max_requests = max(1, int(max_requests))
        self.min_interval_seconds = max(0.2, float(min_interval_seconds))
        self.sleep_fn, self.monotonic_fn = sleep_fn, monotonic_fn
        self._requests = 0
        self._last_request: float | None = None
        self._tripped = False
        self._request_lock = threading.Lock()
        self.reasons_checkpoint_path = (Path(reasons_checkpoint_path)
                                        if reasons_checkpoint_path is not None else None)
        self.members_checkpoint_path = (Path(members_checkpoint_path)
                                        if members_checkpoint_path is not None else None)

    def _client_method(self, *names: str) -> Callable[..., Any]:
        for name in names:
            method = getattr(self.client, name, None)
            if callable(method):
                return method
        raise AttributeError(f"kaipan_method_unavailable:{names[0]}")

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        # Reserve request slots under a lock, but allow the HTTP calls to overlap.
        # This keeps the configured request spacing while bounding member fetch
        # concurrency independently.
        with self._request_lock:
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
                with self._request_lock:
                    self._tripped = True
                raise ProviderCircuitOpen("kaipan_provider_blocked:403") from None
            raise RuntimeError(f"kaipan_request_failed:{type(exc).__name__}") from None
        if isinstance(value, Mapping) and (value.get("status_code") == 403 or
                str(value.get("error", "")).lower() in {"403", "forbidden"}):
            with self._request_lock:
                self._tripped = True
            raise ProviderCircuitOpen("kaipan_provider_blocked:403")
        if value is None or value == [] or value == {}:
            with self._request_lock:
                self._tripped = True
            raise ProviderCircuitOpen("kaipan_empty_response")
        return value

    def limit_up_pool(self, trade_date: str, *, max_levels: int = 10,
                      context: Any = None) -> dict[str, Any]:
        rows, seen = [], set()
        for level in range(1, max_levels + 1):
            try:
                response = self._call(self.client.get_his_bans, trade_date, level)
            except ProviderCircuitOpen as exc:
                if level > 1 and str(exc) == "kaipan_empty_response":
                    self._tripped = False  # no stocks at this board level: end of ladder, not a block
                    break
                raise
            except RuntimeError as exc:
                if level > 1 and str(exc) == "kaipan_request_failed:TypeError":
                    break  # SDK returns None past the highest populated level
                raise
            if (level > 1 and isinstance(response, Mapping) and "info" not in response
                    and str(response.get("errcode")) == "1020"):
                break  # real Kaipan 2026-10-20: errcode 1020 past the highest populated level
            for raw in _pool_rows(response):
                # The legacy pool contract requires the complete positional
                # record (16 fields). Short tuples are metadata/noise and must
                # not become partially trusted pool members.
                if len(raw) < 16:
                    continue
                symbol = _code(raw[0])
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                rows.append({"symbol": symbol, "name": str(raw[1]) if len(raw) > 1 else None,
                             "lb_days": level, "in_legacy_pool": True, "trade_date": trade_date})
        return {"rows": rows, "expected_count": len(seen), "request_count": self._requests}

    def plate_members(self, trade_date: str, *, context: Any = None,
                      page_size: int = 2000, concurrency: int = 4) -> dict[str, Any]:
        if not self.plate_codes:
            raise ValueError("kaipan_plate_codes_required")
        cached = self._load_members_checkpoint(trade_date)
        completed: dict[str, list[dict[str, Any]]] = cached.get("completed", {})
        pending = [plate for plate in self.plate_codes if plate not in completed]
        failures: dict[str, str] = {}

        def fetch_plate(plate: str) -> list[dict[str, Any]]:
            plate_rows: list[dict[str, Any]] = []
            index = 0
            while True:
                response = self._call(self._client_method("get_his_plate_ids", "getHisPlateIds"),
                                      plate, trade_date, str(index), str(page_size))
                page = _list_rows(response)
                for raw in page:
                    if not isinstance(raw, (list, tuple)) or len(raw) < 1:
                        continue
                    symbol = _code(raw[0])
                    if symbol:
                        plate_rows.append({"plate_code": plate, "symbol": symbol,
                                           "plate_class": self.plate_classes.get(plate, "UNKNOWN"),
                                           "plate_member_label": raw[4] if len(raw) > 4 else None,
                                           "trade_date": trade_date})
                count = response.get("Count") if isinstance(response, Mapping) else None
                index += len(page)
                if count is not None and index >= int(count):
                    break
                if not page or len(page) < page_size:
                    if count is not None and index < int(count):
                        raise ValueError("kaipan_plate_member_count_mismatch")
                    break
            return plate_rows

        with ThreadPoolExecutor(max_workers=max(1, min(4, int(concurrency)))) as executor:
            futures = {executor.submit(fetch_plate, plate): plate for plate in pending}
            for future in as_completed(futures):
                plate = futures[future]
                try:
                    completed[plate] = future.result()
                    self._save_members_checkpoint(trade_date, completed)
                except Exception as exc:
                    failures[plate] = type(exc).__name__
        rows = [row for plate in self.plate_codes for row in completed.get(plate, ())]
        failed_plates = [{"plate_code": plate, "error_type": failures[plate]}
                         for plate in self.plate_codes if plate in failures]
        partial = bool(failed_plates or len(completed) != len(self.plate_codes))
        return {"rows": rows, "expected_count": len(rows), "partial": partial,
                "queried_plate_count": len(pending),
                "completed_plate_count": len(completed),
                "failed_plates": failed_plates,
                "unqueried_plate_count": max(0, len(self.plate_codes) - len(completed)),
                "request_count": self._requests}

    def _load_members_checkpoint(self, trade_date: str) -> dict[str, Any]:
        path = self.members_checkpoint_path
        if path is None or not path.is_file():
            return {"completed": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"completed": {}}
        if value.get("trade_date") != trade_date or not isinstance(value.get("completed"), Mapping):
            return {"completed": {}}
        allowed = set(self.plate_codes)
        return {"completed": {str(code): [dict(row) for row in rows if isinstance(row, Mapping)]
                              for code, rows in value["completed"].items()
                              if str(code) in allowed and isinstance(rows, list)}}

    def _save_members_checkpoint(self, trade_date: str,
                                 completed: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        path = self.members_checkpoint_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"trade_date": trade_date, "completed": completed},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def hot_plates(self, trade_date: str, *, context: Any = None,
                   page_size: int = 50) -> dict[str, Any]:
        response = self._call(self._client_method("get_his_plates", "getHisPlates"),
                              trade_date, "0", str(page_size))
        response_metadata = {key: response.get(key) for key in ("Time", "Min", "Max", "Day")
                             if response.get(key) is not None} if isinstance(response, Mapping) else {}
        response_days = response_metadata.get("Day")
        if response_days is not None:
            if not isinstance(response_days, (list, tuple, set)):
                response_days = [response_days]
            normalized_days = {str(day)[:10] for day in response_days if day is not None}
            if normalized_days and trade_date not in normalized_days:
                raise ValueError("kaipan_hot_plates_response_date_mismatch")
        rows = []
        for raw in _list_rows(response)[:page_size]:
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
        return {"rows": rows, "expected_count": len(rows), "request_count": self._requests,
                "requested_date": trade_date, "response_time": response_metadata.get("Time"),
                "response_min": response_metadata.get("Min"),
                "response_max": response_metadata.get("Max"),
                "response_day": response_metadata.get("Day"),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())}

    def plate_minutes(self, plate_code: str, trade_date: str, *, context: Any = None) -> dict[str, Any]:
        code = _code(plate_code)
        if code is None:
            raise ValueError("kaipan_plate_code_invalid")
        response = self._call(self._client_method("get_his_plate_mins", "getHisPlateMins"),
                              code, trade_date)
        if not isinstance(response, Mapping):
            raise ValueError("kaipan_unexpected_plate_minutes_shape")
        trend = response.get("trend")
        if not isinstance(trend, (list, tuple)):
            raise ValueError("kaipan_unexpected_plate_minutes_shape")
        rows = []
        for index, point in enumerate(trend):
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            rows.append({"plate_code": code, "trade_date": trade_date,
                         "time": str(point[0]), "event_time": f"{trade_date}T{point[0]}:00+08:00",
                         "minute_index": index, "price": point[1],
                         "volume": point[2] if len(point) > 2 else None,
                         "amount": point[3] if len(point) > 3 else None})
        metadata = {key: response.get(key) for key in
                    ("begin_px", "hprice", "lprice", "preclose_px", "day")
                    if response.get(key) is not None}
        return {"rows": rows, "expected_count": len(trend), "request_count": self._requests,
                "requested_date": trade_date, "plate_code": code, "metadata": metadata,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())}

    def limit_reasons(self, trade_date: str, *, context: Any = None,
                      symbols: Iterable[str] | None = None) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        done: set[str] = set()
        # getBanReasons returns only a stock's LATEST reason: once it limits up again, the target day's entry is gone.
        # Such symbols are counted as overwritten (missing for this day), never silently as covered.
        overwritten: set[str] = set()
        if self.reasons_checkpoint_path is not None and self.reasons_checkpoint_path.is_file():
            try:
                checkpoint = json.loads(self.reasons_checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                checkpoint = {}
            if checkpoint.get("trade_date") == trade_date:
                done = set(map(str, checkpoint.get("done_symbols", ())))
                rows = [dict(row) for row in checkpoint.get("rows", ()) if isinstance(row, Mapping)]
                overwritten = set(map(str, checkpoint.get("overwritten_symbols", ())))
        requested = tuple(symbols if symbols is not None else self.symbols)
        requested_set = {str(symbol).zfill(6) for symbol in requested}
        rows = [row for row in rows if str(row.get("symbol", "")).zfill(6) in requested_set]
        done.intersection_update(requested_set)
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
            kept = 0
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                event_date = str(entry.get("Date") or "")[:10]
                if event_date and event_date > trade_date:
                    continue
                kept += 1
                rows.append({"symbol": symbol, "event_date": event_date,
                             "type": str(entry.get("Type", "")), "reason": entry.get("Reason"),
                             "plate_codes": entry.get("ZSCode", []),
                             "parent_plate_code": entry.get("PZSCode"), "trade_date": trade_date})
            if entries and not kept:
                overwritten.add(symbol)
            self._save_reasons_checkpoint(trade_date, done, rows, overwritten)
        remaining = [str(symbol).zfill(6) for symbol in requested if str(symbol).zfill(6) not in done]
        overwritten &= requested_set
        return {"rows": rows, "expected_count": len(requested), "coverage_count": len(done - overwritten),
                "queried_symbol_count": queried_now, "overwritten_symbol_count": len(overwritten),
                "unqueried_symbol_count": len(remaining), "partial": bool(remaining or overwritten),
                "request_count": self._requests}

    def _save_reasons_checkpoint(self, trade_date: str, done: set[str],
                                 rows: Sequence[Mapping[str, Any]], overwritten: set[str] = frozenset()) -> None:
        if self.reasons_checkpoint_path is None:
            return
        self.reasons_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.reasons_checkpoint_path.with_name(self.reasons_checkpoint_path.name + ".tmp")
        temporary.write_text(json.dumps({"trade_date": trade_date, "done_symbols": sorted(done),
            "overwritten_symbols": sorted(overwritten), "rows": list(rows)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        os.replace(temporary, self.reasons_checkpoint_path)

