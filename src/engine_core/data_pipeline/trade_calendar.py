"""Cumulative, coverage-aware Core trading calendar."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ..calendar import CalendarCoverageError, parse_trade_date


class CalendarSourceError(RuntimeError):
    pass


class CalendarConflict(RuntimeError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _market_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _months_between(start: date, end: date) -> list[date]:
    cursor = start.replace(day=1)
    result = []
    while cursor <= end:
        result.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def fetch_baostock(start: date, end: date) -> tuple[list[str], bytes, str]:
    try:
        import baostock as bs
        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise CalendarSourceError("BAOSTOCK_LOGIN_FAILED")
        try:
            result = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
            if getattr(result, "error_code", "0") != "0":
                raise CalendarSourceError("BAOSTOCK_QUERY_FAILED")
            rows = []
            while result.next():
                rows.append(result.get_row_data())
            fields = list(getattr(result, "fields", ()))
            if not fields:
                raise CalendarSourceError("BAOSTOCK_SCHEMA_INVALID")
            payload = {"fields": fields, "rows": rows}
            index = {name: fields.index(name) for name in ("calendar_date", "is_trading_day")}
            dates = [row[index["calendar_date"]] for row in rows if row[index["is_trading_day"]] == "1"]
            return dates, _json_bytes(payload), "BS"
        finally:
            bs.logout()
    except CalendarSourceError:
        raise
    except Exception as exc:
        raise CalendarSourceError("BAOSTOCK_UNAVAILABLE") from exc


def fetch_szse_month(year: int, month: int) -> tuple[list[str], bytes]:
    time.sleep(0.25)
    query = urllib.parse.urlencode({"month": f"{year}-{month:02d}", "random": str(time.time())})
    request = urllib.request.Request(
        "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?" + query,
        headers={"User-Agent": "Mozilla/5.0 Chrome/128.0 Safari/537.36", "Referer": "https://www.szse.cn/"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
        payload = json.loads(raw)
        rows = payload.get("data") if isinstance(payload, dict) else payload[0].get("data")
        dates = [row["jyrq"] for row in rows if str(row.get("jybz")) == "1"]
        expected = [(date(year, month, 1) + timedelta(days=offset)).isoformat()
                    for offset in range(monthrange(year, month)[1])]
        if [row.get("jyrq") for row in rows] != expected or any(str(row.get("jybz")) not in {"0", "1"} for row in rows):
            raise CalendarSourceError("SZSE_ROW_COUNT_INVALID")
        return dates, raw
    except CalendarSourceError:
        raise
    except Exception as exc:
        raise CalendarSourceError("SZSE_UNAVAILABLE") from exc


def _validated_baostock_dates(raw: bytes, start: date, end: date, returned: list[str]) -> set[str]:
    try:
        payload = json.loads(raw)
        rows = payload["rows"]
        if rows and isinstance(rows[0], dict):
            row_dates = [row["calendar_date"] for row in rows]
            flags = [str(row["is_trading_day"]) for row in rows]
        else:
            fields = payload["fields"]
            row_dates = [row[fields.index("calendar_date")] for row in rows]
            flags = [str(row[fields.index("is_trading_day")]) for row in rows]
        expected = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        if row_dates != expected or any(flag not in {"0", "1"} for flag in flags):
            raise ValueError("coverage")
        dates = {day for day, flag in zip(row_dates, flags) if flag == "1"}
        if dates != set(returned):
            raise ValueError("trade-date set")
        return dates
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CalendarSourceError("BAOSTOCK_COVERAGE_INVALID") from exc


class TradeCalendarStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("CORE_DATA_ROOT", "~/core-data")).expanduser()
        self.directory = self.root / "trade_calendar"
        self.path = self.directory / "calendar.json"
        self._migrate = True

    @staticmethod
    def _expand(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(entries, key=lambda item: item["d"])

    def load(self) -> dict[str, Any] | None:
        try:
            matrix = json.loads(self.path.read_text(encoding="utf-8"))
            days = matrix["days"]
            if matrix.get("schema") != "CoreTradeCalendarMatrixV1" or not days:
                return None
            if days[0]["d"] != matrix["lit_from"] or days[-1]["d"] != matrix["lit_to"]:
                return None
            return matrix
        except (OSError, ValueError, KeyError, TypeError):
            return self._migrate_latest() if self._migrate else None

    def _migrate_latest(self) -> dict[str, Any] | None:
        try:
            version = (self.directory / "latest").read_text().strip()
            if not version or "/" in version or ".." in version:
                return None
            snapshot = json.loads((self.directory / "versions" / version / "snapshot.json").read_text())
            manifest = json.loads((self.directory / "versions" / version / "manifest.json").read_text())
            dates = snapshot["trading_dates"]
            start, end = snapshot["coverage_start"], snapshot["coverage_end"]
            sources = manifest.get("raw_responses", {})
            dual = any("baostock" in name.lower() for name in sources) and any("szse" in name.lower() for name in sources)
            updated = manifest.get("fetched_at", datetime.now(timezone.utc).isoformat())
            entries = []
            cursor = date.fromisoformat(start)
            while cursor <= date.fromisoformat(end):
                day = cursor.isoformat()
                entries.append({"d": day, "t": int(day in dates), "src": "BS+SZ" if dual else "BS", "u": updated})
                cursor += timedelta(days=1)
            matrix = {"schema": "CoreTradeCalendarMatrixV1", "lit_from": start, "lit_to": end,
                      "last_refresh_at": updated, "last_ok_at": updated, "last_error": None,
                      "conflicts": [], "days": entries}
            _atomic(self.path, _json_bytes(matrix))
            return matrix
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, matrix: dict[str, Any]) -> None:
        _atomic(self.path, _json_bytes(matrix))
        self._export(matrix)

    def _export(self, matrix: dict[str, Any]) -> None:
        dates = [item["d"] for item in matrix["days"] if item["t"]]
        _atomic(self.root / "trade_dates.json", _json_bytes(dates))

    def publish_confirmed(self, dates: Iterable[str], *, source: str, fetched_at: str,
                          coverage_start: str, coverage_end: str,
                          raw_responses: dict[str, bytes], source_rows: int | None = None) -> dict[str, Any]:
        """Compatibility writer for archived confirmations and old callers."""
        del source_rows
        return self._merge({str(parse_trade_date(item)) for item in dates}, set(), None,
                           date.fromisoformat(coverage_start), date.fromisoformat(coverage_end),
                           fetched_at, "BS+SZ" if "SZ" in source.upper() or any("szse" in n.lower() for n in raw_responses) else "BS")

    def _merge(self, bs_days: set[str], sz_days: set[str], scope: set[str] | None, start: date, end: date,
               fetched_at: str, source: str) -> dict[str, Any]:
        today = _market_today().isoformat()
        matrix = self.load()
        old = {item["d"]: item for item in (matrix or {}).get("days", [])}
        conflicts = list((matrix or {}).get("conflicts", []))
        merged = dict(old)
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            if scope is not None and key not in scope:
                cursor += timedelta(days=1)
                continue
            b, s = key in bs_days, key in sz_days
            if source == "BS+SZ":
                # publish_confirmed is already cross-validated by its caller.
                value, src = b, source
            elif b and s:
                value, src = True, "BS+SZ"
            elif b or s:
                value, src = b or s, "BS" if b else "SZ"
            else:
                # source membership sets encode open days only; scope enables closed days
                value, src = False, "BS+SZ" if source == "BS+SZ" else source
            prior = old.get(key)
            if key < today and prior:
                if int(prior["t"]) != int(value):
                    conflicts.append({"d": key, "old": int(prior["t"]), "new": int(value), "at": fetched_at})
                cursor += timedelta(days=1)
                continue
            merged[key] = {"d": key, "t": int(value), "src": src, "u": fetched_at}
            cursor += timedelta(days=1)
        all_days = self._expand(list(merged.values()))
        if not all_days:
            raise CalendarSourceError("EMPTY_CALENDAR")
        matrix = {"schema": "CoreTradeCalendarMatrixV1", "lit_from": all_days[0]["d"],
                  "lit_to": all_days[-1]["d"], "last_refresh_at": fetched_at, "last_ok_at": fetched_at,
                  "last_error": None, "conflicts": conflicts[-500:], "days": all_days}
        self._write(matrix)
        return {"status": "CONFIRMED", "reason_code": None, "lit_from": matrix["lit_from"],
                "lit_to": matrix["lit_to"], "last_ok_at": matrix["last_ok_at"]}

    def refresh(self, *, now: date | None = None, window_days: int = 30,
                baostock_fetch: Callable[[date, date], tuple[list[str], bytes, str]] = fetch_baostock,
                szse_fetch: Callable[[int, int], tuple[list[str], bytes]] = fetch_szse_month) -> dict[str, Any]:
        today = now or _market_today()
        start = date(2026, 1, 1) if not self.load() else today - timedelta(days=window_days)
        end = today + timedelta(days=window_days)
        refreshed = datetime.now(timezone.utc).isoformat()
        bao: set[str] | None = None
        szse: set[str] | None = None
        errors = []
        try:
            values, raw, _ = baostock_fetch(start, end)
            returned = {str(parse_trade_date(item)) for item in values}
            bao = _validated_baostock_dates(raw, start, end, sorted(returned))
        except Exception as exc:
            errors.append(exc.args[0] if isinstance(exc, CalendarSourceError) else "BAOSTOCK_UNAVAILABLE")
        try:
            szse = set()
            for month in _months_between(start, end):
                for attempt in range(3):  # a transient network error must not discard the whole second source
                    try:
                        values, _ = szse_fetch(month.year, month.month)
                        break
                    except CalendarSourceError:
                        if attempt == 2:
                            raise
                        time.sleep(1.5 * (attempt + 1))
                szse.update(str(parse_trade_date(item)) for item in values)
        except Exception as exc:
            szse = None
            errors.append(exc.args[0] if isinstance(exc, CalendarSourceError) else "SZSE_UNAVAILABLE")
        matrix = self.load()
        if bao is None and szse is None:
            return {"status": "STALE" if matrix else "UNKNOWN", "reason_code": errors[0] if errors else "REFRESH_FAILED"}
        scope = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        if bao is not None and szse is not None:
            conflicts = {key for key in scope if (key in bao) != (key in szse)}
            if conflicts == scope and not matrix:
                # No agreed dates exist to illuminate on a cold start.
                return {"status": "PARTIAL", "reason_code": "SOURCE_CONFLICT", "lit_from": None,
                        "lit_to": None, "last_ok_at": None}
            result = self._merge(bao, szse, scope - conflicts, start, end, refreshed, "BS+SZ")
            if conflicts:
                loaded = self.load()
                loaded["conflicts"].extend({"d": key, "bs": int(key in bao), "sz": int(key in szse), "at": refreshed}
                                           for key in sorted(conflicts))
                loaded["conflicts"] = loaded["conflicts"][-500:]
                self._write(loaded)
                result.update(status="PARTIAL", reason_code="SOURCE_CONFLICT")
            return result
        values = bao if bao is not None else szse
        result = self._merge(values or set(), set(), scope, start, end, refreshed, "BS" if bao is not None else "SZ")
        result.update(status="PARTIAL", reason_code=errors[0] if errors else "SINGLE_SOURCE")
        return result

    def initialize(self, *, now: date | None = None, **fetchers: Any) -> dict[str, Any]:
        """First run: with no matrix file, light the calendar from a cold start (real fetch, no legacy migration)."""
        if self.path.exists():
            return {"status": "EXISTS", "reason_code": "MATRIX_PRESENT"}
        self._migrate = False
        try:
            result = self.refresh(now=now, **fetchers)
        finally:
            self._migrate = True
        return result

    def force_refresh(self, *, now: date | None = None, **fetchers: Any) -> dict[str, Any]:
        """Forced update: fetch the window now regardless of age or lit-ahead; failures leave the file unchanged."""
        return self.refresh(now=now, **fetchers)

    def refresh_if_needed(self, *, now: datetime | None = None) -> dict[str, Any]:
        local = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() if now and now.tzinfo else
                 now.date() if now else _market_today())
        if not self.path.exists() and self._migrate:
            first = self.initialize(now=local)   # first run: light from real sources, do not adopt legacy versions
            if first.get("status") == "CONFIRMED" or self.path.exists():
                return first
        status = self.calendar_status(local)
        matrix = self.load()
        age = 999 if not matrix else max(0, (datetime.now(timezone.utc) -
            datetime.fromisoformat(matrix["last_ok_at"].replace("Z", "+00:00")).astimezone(timezone.utc)).days)
        if not matrix or age > 7 or status["lit_days_ahead"] is None or status["lit_days_ahead"] < 20:
            return self.refresh(now=local)
        return {**status, "status": "CONFIRMED"}

    def calendar_status(self, now: date | datetime | None = None) -> dict[str, Any]:
        today = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() if now.tzinfo else now.date()) \
            if isinstance(now, datetime) else (now or _market_today())
        matrix = self.load()
        if not matrix:
            return {"status": "UNKNOWN", "as_of": today.isoformat(), "is_trading_day": None,
                    "lit_from": None, "lit_to": None, "lit_days_ahead": None, "last_ok_at": None, "conflicts": 0}
        covered = matrix["lit_from"] <= today.isoformat() <= matrix["lit_to"]
        ahead = (date.fromisoformat(matrix["lit_to"]) - today).days
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(matrix["last_ok_at"].replace("Z", "+00:00")).astimezone(timezone.utc)).days)
        status = "UNKNOWN" if not covered else "STALE" if age > 7 or ahead < 7 or matrix.get("last_error") else "CONFIRMED"
        day_map = {item["d"]: item["t"] for item in matrix["days"]}
        return {"status": status, "as_of": today.isoformat(), "is_trading_day": bool(day_map[today.isoformat()])
                if covered and today.isoformat() in day_map else None,
                "lit_from": matrix["lit_from"], "lit_to": matrix["lit_to"], "lit_days_ahead": ahead if covered else None,
                "last_ok_at": matrix["last_ok_at"], "conflicts": len(matrix.get("conflicts", []))}

    def _covered(self, value: date | str) -> tuple[dict[str, Any], str]:
        parsed = parse_trade_date(value).isoformat()
        data = self.load()
        if (not data or not data["lit_from"] <= parsed <= data["lit_to"] or
                not any(item["d"] == parsed for item in data["days"])):
            raise CalendarCoverageError("calendar coverage UNKNOWN")
        return data, parsed

    def is_trading_day(self, value: date | str) -> bool:
        data, parsed = self._covered(value)
        return bool(next(item["t"] for item in data["days"] if item["d"] == parsed))

    def previous_trade_date(self, value: date | str) -> date:
        data, parsed = self._covered(value)
        prior = [item["d"] for item in data["days"] if item["t"] and item["d"] < parsed]
        if not prior:
            raise CalendarCoverageError("previous trade date UNKNOWN")
        return date.fromisoformat(prior[-1])

    def next_trade_date(self, value: date | str) -> date:
        data, parsed = self._covered(value)
        following = [item["d"] for item in data["days"] if item["t"] and item["d"] > parsed]
        if not following:
            raise CalendarCoverageError("next trade date UNKNOWN")
        return date.fromisoformat(following[0])

    def trading_dates(self, start: date | str, end: date | str) -> list[str]:
        left, right = parse_trade_date(start).isoformat(), parse_trade_date(end).isoformat()
        data, _ = self._covered(left)
        self._covered(right)
        return [item["d"] for item in data["days"] if item["t"] and left <= item["d"] <= right]

    def all_trading_dates(self) -> list[str]:
        data = self.load()
        return [item["d"] for item in data["days"] if item["t"]] if data else []


def is_trading_day(value: date | str, *, root: str | Path | None = None) -> bool:
    return TradeCalendarStore(root).is_trading_day(value)


def previous_trade_date(value: date | str, *, root: str | Path | None = None) -> date:
    return TradeCalendarStore(root).previous_trade_date(value)


def next_trade_date(value: date | str, *, root: str | Path | None = None) -> date:
    return TradeCalendarStore(root).next_trade_date(value)


def trading_dates(start: date | str, end: date | str, *, root: str | Path | None = None) -> list[str]:
    return TradeCalendarStore(root).trading_dates(start, end)


def calendar_status(now: date | datetime | None = None, *, root: str | Path | None = None) -> dict[str, Any]:
    return TradeCalendarStore(root).calendar_status(now)
