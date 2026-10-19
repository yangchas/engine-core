"""Versioned Core trading calendar with fail-closed reads and explicit refresh."""
from __future__ import annotations

import hashlib
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _months_between(start: date, end: date) -> list[date]:
    cursor = _month_start(start)
    result = []
    while cursor <= end:
        result.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def _market_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _shift_month(value: date, offset: int) -> date:
    ordinal = value.year * 12 + value.month - 1 + offset
    year, month = divmod(ordinal, 12)
    month += 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


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
            return dates, _json_bytes(payload), "BaoStock"
        finally:
            bs.logout()
    except CalendarSourceError:
        raise
    except Exception as exc:
        raise CalendarSourceError("BAOSTOCK_UNAVAILABLE") from exc


def fetch_szse_month(year: int, month: int) -> tuple[list[str], bytes]:
    # Public endpoint observed with a 250 ms spacing in the C325 capture.
    time.sleep(0.25)
    query = urllib.parse.urlencode({"month": f"{year}-{month:02d}", "random": str(time.time())})
    request = urllib.request.Request(
        "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?" + query,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
                 "Referer": "https://www.szse.cn/"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
        payload = json.loads(raw)
        rows = payload.get("data") if isinstance(payload, dict) else payload[0].get("data")
        dates = [row["jyrq"] for row in rows if str(row.get("jybz")) == "1"]
        row_dates = [row.get("jyrq") for row in rows]
        row_flags = [str(row.get("jybz")) for row in rows]
        expected = [date(year, month, day).isoformat()
                    for day in range(1, monthrange(year, month)[1] + 1)]
        if row_dates != expected or any(flag not in {"0", "1"} for flag in row_flags):
            raise CalendarSourceError("SZSE_ROW_COUNT_INVALID")
        return dates, raw
    except CalendarSourceError:
        raise
    except Exception as exc:
        raise CalendarSourceError("SZSE_UNAVAILABLE") from exc


class TradeCalendarStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("CORE_DATA_ROOT", "~/core-data")).expanduser()
        self.directory = self.root / "trade_calendar"

    def _latest_version(self) -> str | None:
        try:
            value = (self.directory / "latest").read_text().strip()
            return value if value and "/" not in value and ".." not in value else None
        except OSError:
            return None

    def load(self) -> dict[str, Any] | None:
        version = self._latest_version()
        if not version:
            return None
        try:
            data = json.loads((self.directory / "versions" / version / "snapshot.json").read_text())
            dates = data["trading_dates"]
            if not isinstance(dates, list) or dates != sorted(set(dates)):
                return None
            manifest = json.loads((self.directory / "versions" / version / "manifest.json").read_text())
            snapshot_bytes = _json_bytes(data)
            if manifest.get("snapshot_sha256") != _sha(snapshot_bytes):
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def publish_confirmed(self, dates: Iterable[str], *, source: str, fetched_at: str,
                          coverage_start: str, coverage_end: str,
                          raw_responses: dict[str, bytes], source_rows: int | None = None) -> dict[str, Any]:
        candidate = sorted(set(str(parse_trade_date(item)) for item in dates))
        if not candidate or any(not coverage_start <= item <= coverage_end for item in candidate):
            raise CalendarSourceError("CANDIDATE_COVERAGE_INVALID")
        previous = self.load()
        previous_dates = previous["trading_dates"] if previous else []
        prior_map = set(previous_dates)
        new_map = set(candidate)
        # Historical source corrections require manual review. Keep old history and
        # only append genuinely unseen dates after confirming the overlap.
        market_today = _market_today().isoformat()
        old_window = {item for item in prior_map if coverage_start <= item <= coverage_end and
                      item <= market_today}
        new_window = {item for item in new_map if coverage_start <= item <= coverage_end and
                      item <= market_today}
        if previous and old_window != new_window:
            self._save_pending(candidate, source, fetched_at, coverage_start, coverage_end, raw_responses,
                               "HISTORICAL_CONFLICT")
            raise CalendarConflict("HISTORICAL_CONFLICT")
        # Source window must not delete older calendar history.
        window_prior = {item for item in prior_map if coverage_start <= item <= coverage_end}
        merged = sorted((prior_map - window_prior) | new_map)
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        raw_meta = {name: {"sha256": _sha(value), "bytes": len(value)} for name, value in raw_responses.items()}
        snapshot = {"schema": "CoreTradeCalendarV1", "version": version,
                    "coverage_start": min(previous.get("coverage_start", coverage_start), coverage_start) if previous else coverage_start,
                    "coverage_end": max(previous.get("coverage_end", coverage_end), coverage_end) if previous else coverage_end,
                    "trading_dates": merged}
        diffs = {"added": sorted(new_map - prior_map), "removed": sorted(window_prior - new_map),
                 "retained": len(new_map & prior_map)}
        manifest = {"schema": "CoreTradeCalendarManifestV1", "version": version,
                    "status": "CONFIRMED", "source": source, "fetched_at": fetched_at,
                    "available_at": None,
                    "window": {"start": snapshot["coverage_start"], "end": snapshot["coverage_end"]},
                    "refresh_window": {"start": coverage_start, "end": coverage_end},
                    "coverage_start": snapshot["coverage_start"],
                    "coverage_end": snapshot["coverage_end"],
                    "row_count": len(merged), "candidate_row_count": source_rows or len(candidate),
                    "raw_responses": raw_meta, "snapshot_sha256": _sha(_json_bytes(snapshot)),
                    "semantic_sha256": _sha(_json_bytes(merged)),
                    "diff_from_previous": diffs, "validation": {"status": "AGREED",
                    "confirmed_segments": [{"start": coverage_start, "end": coverage_end,
                        "sources": sorted(raw_meta),
                        "evidence_sha256": {k: v["sha256"] for k, v in raw_meta.items()}}]}}
        version_dir = self.directory / "versions" / version
        version_dir.mkdir(parents=True, exist_ok=False)
        for name, raw in raw_responses.items():
            _atomic(version_dir / "raw" / Path(name).name, raw)
        _atomic(version_dir / "snapshot.json", _json_bytes(snapshot))
        _atomic(version_dir / "manifest.json", _json_bytes(manifest))
        _atomic(self.directory / "latest", (version + "\n").encode())
        _atomic(self.root / "trade_dates.json", _json_bytes(merged))
        return {**manifest, "trading_dates": merged}

    def _save_pending(self, dates: list[str], source: str, fetched_at: str, start: str, end: str,
                      raw: dict[str, bytes], reason: str) -> None:
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-pending"
        directory = self.directory / "pending-review" / version
        directory.mkdir(parents=True, exist_ok=False)
        for name, body in raw.items():
            _atomic(directory / "raw" / Path(name).name, body)
        manifest = {"version": version, "status": "PENDING_REVIEW", "reason_code": reason,
                    "source": source, "fetched_at": fetched_at,
                    "window": {"start": start, "end": end}, "row_count": len(dates),
                    "raw_sha256": {name: _sha(body) for name, body in raw.items()}}
        _atomic(directory / "manifest.json", _json_bytes(manifest))
        _atomic(directory / "snapshot.json", _json_bytes({"trading_dates": dates}))

    def refresh(self, *, now: date | None = None, window_days: int = 30,
                baostock_fetch: Callable[[date, date], tuple[list[str], bytes, str]] = fetch_baostock,
                szse_fetch: Callable[[int, int], tuple[list[str], bytes]] = fetch_szse_month) -> dict[str, Any]:
        today = now or _market_today()
        if window_days == 30:
            start, end = _shift_month(today, -1), _shift_month(today, 1)
        else:
            start, end = today - timedelta(days=window_days), today + timedelta(days=window_days)
        previous = self.load()
        fetch_start = date(2026, 1, 1) if not previous else start
        fetched_at = datetime.now(timezone.utc).isoformat()
        raw: dict[str, bytes] = {}
        try:
            candidate_dates: list[str] = []
            bao_dates, bao_raw, _ = baostock_fetch(fetch_start, end)
            candidate_dates = list(bao_dates)
            raw["baostock.json"] = bao_raw
            bao_payload = json.loads(bao_raw)
            bao_rows = bao_payload.get("rows", [])
            expected_days = [(fetch_start + timedelta(days=offset)).isoformat()
                             for offset in range((end - fetch_start).days + 1)]
            row_dates = [row[bao_payload["fields"].index("calendar_date")] for row in bao_rows]
            flags = [row[bao_payload["fields"].index("is_trading_day")] for row in bao_rows]
            if row_dates != expected_days or any(flag not in {"0", "1"} for flag in flags):
                raise CalendarSourceError("BAOSTOCK_COVERAGE_INVALID")
            szse_dates: set[str] = set()
            for month in _months_between(fetch_start, end):
                values, body = szse_fetch(month.year, month.month)
                szse_dates.update(values)
                raw[f"szse-{month.year}-{month.month:02d}.json"] = body
            queried_window_bao = {d for d in bao_dates if fetch_start.isoformat() <= d <= end.isoformat()}
            queried_window_szse = {d for d in szse_dates if fetch_start.isoformat() <= d <= end.isoformat()}
            if queried_window_bao != queried_window_szse:
                self._save_pending(sorted(set(bao_dates) | szse_dates), "BaoStock+SZSE", fetched_at,
                                   fetch_start.isoformat(), end.isoformat(), raw, "SOURCE_CONFLICT")
                return {"status": "PENDING_REVIEW", "reason_code": "SOURCE_CONFLICT",
                        "version": self._latest_version()}
            if not previous:
                legacy_path = self.root / "trade_dates.json"
                if legacy_path.is_file():
                    try:
                        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                        legacy_dates = set(legacy if isinstance(legacy, list) else legacy["trading_dates"])
                        lo, hi = min(legacy_dates), max(legacy_dates)
                        fetched_legacy_span = {d for d in bao_dates if lo <= d <= hi}
                        if fetched_legacy_span != legacy_dates:
                            self._save_pending(sorted(set(bao_dates)), "BaoStock+SZSE", fetched_at,
                                               fetch_start.isoformat(), end.isoformat(), raw,
                                               "LEGACY_CROSSCHECK_CONFLICT")
                            return {"status": "PENDING_REVIEW", "reason_code": "LEGACY_CROSSCHECK_CONFLICT",
                                    "version": self._latest_version()}
                    except (OSError, ValueError, TypeError, KeyError):
                        return {"status": "UNKNOWN", "reason_code": "LEGACY_CROSSCHECK_INVALID",
                                "version": None}
            dates = set(bao_dates)
            candidate_dates = sorted(dates)
            if previous:
                old = set(previous["trading_dates"])
                historical_window = {d for d in dates if d <= today.isoformat()}
                if {d for d in old if fetch_start.isoformat() <= d <= today.isoformat()} != historical_window & old:
                    self._save_pending(sorted(dates), "BaoStock+SZSE", fetched_at,
                                       fetch_start.isoformat(), end.isoformat(), raw, "HISTORICAL_CONFLICT")
                    return {"status": "PENDING_REVIEW", "reason_code": "HISTORICAL_CONFLICT",
                            "version": self._latest_version()}
            return self.publish_confirmed(dates, source="BaoStock+SZSE", fetched_at=fetched_at,
                                          coverage_start=fetch_start.isoformat(), coverage_end=end.isoformat(),
                                          raw_responses=raw, source_rows=len(bao_dates))
        except CalendarConflict:
            return {"status": "PENDING_REVIEW", "reason_code": "HISTORICAL_CONFLICT",
                    "version": self._latest_version()}
        except Exception as exc:
            reason = exc.args[0] if isinstance(exc, CalendarSourceError) else "REFRESH_FAILED"
            self._save_pending(candidate_dates if "candidate_dates" in locals() else [],
                               "BaoStock+SZSE", fetched_at, fetch_start.isoformat(),
                               end.isoformat(), raw, reason)
            return {"status": "STALE" if previous else "UNKNOWN", "reason_code": reason,
                    "version": self._latest_version()}

    def refresh_if_needed(self, *, now: datetime | None = None) -> dict[str, Any]:
        local = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai")).date() \
            if (now or datetime.now(ZoneInfo("Asia/Shanghai"))).tzinfo else (now or _market_today())
        status = self.calendar_status(local)
        if status["status"] == "UNKNOWN" or status["age_days"] > 7 or status["coverage_end"] is None or \
                status["coverage_end"] < (local + timedelta(days=20)).isoformat():
            return self.refresh(now=local)
        return status

    def calendar_status(self, now: date | datetime | None = None) -> dict[str, Any]:
        today = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() if now.tzinfo else now.date()) \
            if isinstance(now, datetime) else (now or _market_today())
        data = self.load()
        if not data:
            return {"status": "UNKNOWN", "as_of": today.isoformat(), "is_trading_day": None,
                    "version": None, "age_days": None,
                    "coverage_start": None, "coverage_end": None}
        version = data["version"]
        try:
            manifest = json.loads((self.directory / "versions" / version / "manifest.json").read_text())
            fetched = datetime.fromisoformat(manifest["fetched_at"].replace("Z", "+00:00"))
            age = max(0, (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).days)
        except Exception:
            manifest, age = {}, None
        start, end = data.get("coverage_start"), data.get("coverage_end")
        status = "CONFIRMED" if manifest.get("status") == "CONFIRMED" else "UNKNOWN"
        if age is None or age > 7 or not start or not end or end < (today + timedelta(days=20)).isoformat():
            status = "STALE" if status == "CONFIRMED" else "UNKNOWN"
        return {"status": status, "as_of": today.isoformat(),
                "is_trading_day": today.isoformat() in data["trading_dates"]
                if start and end and start <= today.isoformat() <= end else None,
                "version": version, "age_days": age,
                "coverage_start": start, "coverage_end": end,
                "window": {"start": start, "end": end},
                "trading_dates": data["trading_dates"],
                "trading_date_count": len(data["trading_dates"])}

    def _covered(self, value: date | str) -> tuple[dict[str, Any], str]:
        parsed = parse_trade_date(value).isoformat()
        data = self.load()
        if not data or not data.get("coverage_start", "9999") <= parsed <= data.get("coverage_end", "0000"):
            raise CalendarCoverageError("calendar coverage UNKNOWN")
        return data, parsed

    def is_trading_day(self, value: date | str) -> bool:
        data, parsed = self._covered(value)
        return parsed in data["trading_dates"]

    def previous_trade_date(self, value: date | str) -> date:
        data, parsed = self._covered(value)
        prior = [d for d in data["trading_dates"] if d < parsed]
        if not prior:
            raise CalendarCoverageError("previous trade date UNKNOWN")
        return date.fromisoformat(prior[-1])

    def next_trade_date(self, value: date | str) -> date:
        data, parsed = self._covered(value)
        following = [d for d in data["trading_dates"] if d > parsed]
        if not following:
            raise CalendarCoverageError("next trade date UNKNOWN")
        return date.fromisoformat(following[0])

    def trading_dates(self, start: date | str, end: date | str) -> list[str]:
        left, right = parse_trade_date(start).isoformat(), parse_trade_date(end).isoformat()
        data, _ = self._covered(left)
        self._covered(right)
        return [d for d in data["trading_dates"] if left <= d <= right]

    def all_trading_dates(self) -> list[str]:
        data = self.load()
        return list(data["trading_dates"]) if data else []


_DEFAULT = TradeCalendarStore()


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
