"""Bounded, read-only WenCai adapter for historical daily K-line rows."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_FILE = "~/core-data/secrets/wencai.cookie"
DEFAULT_SEGMENT_EDGES_YI: tuple[float | None, ...] = (0, 15, 25, 35, 50, 80, 150, 500, None)
_UNIT_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "volume", "amount_yuan")
_CHECKPOINT_MAX_AGE_SECONDS = 6 * 3600


PAGE_SIZE = 100  # upstream caps perpage at 100 (1000 returns 30 rows or an error)
MAX_PAGES = 80
_FIELDS = "开盘价，最高价，最低价，收盘价，前收盘价，涨跌幅，成交量(股)，成交额(元)，股票代码"
_DATE_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "volume", "amount_yuan")


def build_daily_kline_query(trade_date: str) -> str:
    """Use a concise date-led query; verbose market prose can select latest-day columns."""
    day = date.fromisoformat(trade_date).isoformat()
    return f"{day}{_FIELDS}"


def _records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, Mapping):
        if "rows" in result:
            return _records(result["rows"])
        if "data" in result:
            return _records(result["data"])
        return [dict(result)]
    if hasattr(result, "to_dict"):
        try:
            return [dict(row) for row in result.to_dict(orient="records") if isinstance(row, Mapping)]
        except (TypeError, ValueError):
            return []
    try:
        return [dict(row) for row in result if isinstance(row, Mapping)]
    except TypeError:
        return []


def _field_value(row: Mapping[str, Any], field: str, trade_date: str) -> Any:
    """Select the requested day's dated source column, ignoring undated latest fields."""
    expected = trade_date.replace("-", "")
    candidates = []
    for header, value in row.items():
        if _field_for_header(header) != field:
            continue
        match = re.search(r"\[(\d{8})\]$", str(header))
        if match:
            candidates.append((match.group(1), str(header), value))
        elif not any(token in str(header) for token in ("最新", "现价")):
            candidates.append((None, str(header), value))
    dated = [item for item in candidates if item[0] == expected]
    if not dated:
        dated = [item for item in candidates if item[0] is not None]
    if not dated:
        dated = [item for item in candidates if item[0] is None]
    if not dated:
        return None

    def priority(item: tuple[str | None, str, Any]) -> tuple[int, str]:
        header = item[1]
        if field in {"open", "high", "low", "close", "pre_close"}:
            adjustment_rank = 0 if "不复权" in header else 1 if "前复权" in header else 2
        elif field == "pct_chg":
            adjustment_rank = 0 if "不复权" in header else 1 if "前复权" in header else 2
        else:
            adjustment_rank = 0
        return adjustment_rank, header

    return sorted(dated, key=priority)[0][2]


def _field_for_header(header: Any) -> str | None:
    value = re.sub(r"\[\d{8}\]$", "", str(header)).lower().replace(" ", "")
    # WenCai may append live quote fields beside the requested historical
    # columns. They do not carry a date suffix and are not date evidence.
    if any(token in value for token in ("最新", "现价")):
        return None
    if any(item in value for item in ("前收盘", "昨收", "pre_close", "preclose")):
        return "pre_close"
    aliases = {
        "open": ("开盘", "open"), "high": ("最高", "high"),
        "low": ("最低", "low"), "close": ("收盘", "close"),
        "pct_chg": ("涨跌幅", "涨幅", "pct_chg", "pctchg"),
        "volume": ("成交量", "volume"),
        "amount_yuan": ("成交额", "成交金额", "amount"),
    }
    for field, words in aliases.items():
        if any(word in value for word in words):
            return field
    return None


def _source_dates(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    found: dict[str, set[str]] = {}
    missing: set[str] = set()
    all_dates: set[str] = set()
    for row in rows:
        for header in row:
            match = re.search(r"\[(\d{8})\]$", str(header))
            field = _field_for_header(header)
            if field and match:
                value = f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
                found.setdefault(field, set()).add(value)
                all_dates.add(value)
            elif field:
                missing.add(field)
    source_dates = {
        field: (None if field in missing or not found.get(field) else
                sorted(found[field])[0] if len(found[field]) == 1 else sorted(found[field]))
        for field in _DATE_FIELDS
    }
    return source_dates, sorted(all_dates)


def _source_units(rows: list[dict[str, Any]]) -> dict[str, str]:
    headers = [str(key).lower().replace(" ", "") for row in rows for key in row]
    volume = "shares" if "(股)" in _FIELDS else "UNKNOWN"
    amount = "yuan" if "(元)" in _FIELDS else "UNKNOWN"
    for header in headers:
        if _field_for_header(header) == "volume":
            if "万手" in header:
                volume = "wan_hands"
            elif "手" in header:
                volume = "hands"
            elif "万股" in header:
                volume = "wan_shares"
            elif "股" in header:
                volume = "shares"
        elif _field_for_header(header) == "amount_yuan":
            if "亿元" in header:
                amount = "yi_yuan"
            elif "万元" in header:
                amount = "wan_yuan"
            elif "元" in header:
                amount = "yuan"
    return {"price": "yuan", "pct_chg": "percent", "amount_yuan": amount,
            "volume": volume}


class _Stop(Exception):
    """Internal: the request or wall budget is exhausted; keep what was fetched."""


_FORBIDDEN = re.compile(r"403|forbidden", re.I)


def _read_cookie(cookie_file: str | os.PathLike[str] | None) -> str | None:
    """Return the cookie text, or None when absent/empty. The value is never logged or stored."""
    if not cookie_file:
        return None
    try:
        text = Path(cookie_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _redact(text: Any, secret: str | None) -> str:
    value = str(text)
    return value.replace(secret, "<cookie>") if secret else value


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return str(value)


def _symbol_of_raw(row: Mapping[str, Any]) -> str | None:
    from ...wencai_fallback import _symbol
    return _symbol(row)


def _filled_fields(row: Mapping[str, Any], trade_date: str) -> int:
    from ...wencai_fallback import _number
    return sum(_number(_field_value(row, field, trade_date)) is not None for field in _UNIT_FIELDS)


def dedupe_rows(rows: Iterable[dict[str, Any]], trade_date: str) -> tuple[list[dict[str, Any]], int]:
    """One row per symbol; the C563 rule: most non-null fields, then smallest canonical JSON."""
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for row in rows:
        symbol = _symbol_of_raw(row)
        if symbol is None:
            order.append(row)
            continue
        if symbol not in groups:
            groups[symbol] = []
            order.append(symbol)
        groups[symbol].append(row)
    out: list[dict[str, Any]] = []
    dropped = 0
    for item in order:
        if not isinstance(item, str):
            out.append(item)
            continue
        candidates = groups[item]
        dropped += len(candidates) - 1
        candidates.sort(key=lambda r: (-_filled_fields(r, trade_date),
                                       json.dumps(r, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":"), default=_json_default)))
        out.append(candidates[0])
    return out, dropped


class _Run:
    """Shared state of one fetch: request budget, cooldown policy, cookie, page checkpoint."""

    def __init__(self, *, query_fn, context, page_size, cookie, cooldown_seconds, cooldown_factor,
                 cooldown_max_seconds, max_cooldowns, max_requests, sleep_fn, clock_fn,
                 wall_budget_seconds, checkpoint_path, trade_date, question):
        self.query_fn, self.context, self.page_size = query_fn, context, page_size
        self.cookie = cookie
        self.cookie_status = "used" if cookie else "absent"
        self.cookie_length = len(cookie) if cookie else 0
        self.cooldown_seconds, self.cooldown_factor = cooldown_seconds, cooldown_factor
        self.cooldown_max_seconds, self.max_cooldowns = cooldown_max_seconds, max_cooldowns
        self.max_requests, self.sleep_fn, self.clock_fn = max_requests, sleep_fn, clock_fn
        self.deadline = None if wall_budget_seconds is None else clock_fn() + wall_budget_seconds
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.trade_date, self.question = trade_date, question
        self.requests = 0
        self.cooldowns = 0
        self.units: dict[str, Any] = {}
        self._dirty = False
        self._load_checkpoint()

    def _load_checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        try:
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            fresh = time.time() - float(data.get("saved_at", 0)) < _CHECKPOINT_MAX_AGE_SECONDS
            if (fresh and data.get("trade_date") == self.trade_date
                    and data.get("question") == self.question and data.get("page_size") == self.page_size
                    and isinstance(data.get("units"), dict)):
                self.units = data["units"]
        except (OSError, ValueError, TypeError):
            self.units = {}

    def save_checkpoint(self, force: bool = False) -> None:
        if self.checkpoint_path is None or not (self._dirty or force):
            return
        body = {"trade_date": self.trade_date, "question": self.question, "page_size": self.page_size,
                "saved_at": time.time(), "units": self.units}
        try:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, default=_json_default), encoding="utf-8")
            os.replace(tmp, self.checkpoint_path)
            self._dirty = False
        except OSError:
            pass

    def clear_checkpoint(self) -> None:
        if self.checkpoint_path is not None:
            try:
                self.checkpoint_path.unlink()
            except OSError:
                pass

    def remember(self, key: str, value: Any) -> None:
        self.units[key] = value
        self._dirty = True

    def cooldown(self, attempt: int) -> None:
        delay = min(self.cooldown_max_seconds, self.cooldown_seconds * (self.cooldown_factor ** attempt))
        if self.deadline is not None and self.clock_fn() + delay >= self.deadline:
            raise _Stop("wall_budget_exhausted")
        self.save_checkpoint()
        self.cooldowns += 1
        self.sleep_fn(delay)

    def call(self, question: str, page: int, *, anonymous: bool = False) -> Any:
        if self.requests >= self.max_requests:
            raise _Stop("request_budget_exhausted")
        if self.deadline is not None and self.clock_fn() >= self.deadline:
            raise _Stop("wall_budget_exhausted")
        if self.requests >= 1 and self.context is not None:
            self.context.request()   # the provider accounts for the very first request itself
        self.requests += 1
        kwargs: dict[str, Any] = dict(query=question, page=page, perpage=self.page_size,
                                      query_type="stock", sort_key="股票代码", sort_order="asc",
                                      retry=0, strict=True)
        if self.cookie and not anonymous:
            kwargs["cookie"] = self.cookie
        return self.query_fn(**kwargs)


class _Query:
    """Outcome of paging one query: rows by page, reported total, and why it stopped."""

    def __init__(self):
        self.pages: dict[int, list[dict[str, Any]]] = {}
        self.total: int | None = None
        self.error: Exception | None = None
        self.stop_reason: str | None = None
        self.oversize = False
        self.truncated = False

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [row for page in sorted(self.pages) for row in self.pages[page]]

    @property
    def complete(self) -> bool:
        if self.error is not None or self.stop_reason or self.truncated:
            return False
        return self.total is None or len(self.rows) >= self.total


def _page_total(result: Any, current: int | None) -> int | None:
    if isinstance(result, Mapping) and result.get("row_count") is not None:
        try:
            return int(result["row_count"])
        except (TypeError, ValueError):
            pass
    return current


def _expected_pages(total: int | None, page_size: int) -> int | None:
    return None if total is None else max(1, (total + page_size - 1) // page_size)


def _fetch_query(run: _Run, question: str, *, unit_prefix: str, max_pages: int,
                 stop_if_total_over: int | None = None, first_unit: bool = False) -> _Query:
    """Page one query. A missing/short page before the reported end is a cooldown-and-retry of the
    SAME page (never a restart); pages already fetched (also from the checkpoint) are kept."""
    out = _Query()
    page = 1
    while page <= max_pages:
        key = f"{unit_prefix}:{page}"
        cached = run.units.get(key)
        if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
            out.pages[page] = cached["rows"]
            out.total = cached.get("total", out.total)
            expected = _expected_pages(out.total, run.page_size)
            if expected is not None and page >= expected:
                break
            page += 1
            continue
        attempt = 0
        while True:
            result: Any = None
            failure: Exception | None = None
            try:
                result = run.call(question, page)
            except _Stop as stop:
                out.stop_reason = str(stop)
                return out
            except Exception as exc:  # noqa: BLE001 - classified below
                failure = exc
            if (first_unit and page == 1 and run.cookie_status == "used" and (
                    (failure is not None and not _FORBIDDEN.search(str(failure))
                     and not isinstance(failure, TimeoutError))
                    or (failure is None and not _records(result)))):
                # Possibly an expired login: retry once anonymously; only if that works is the cookie to blame.
                try:
                    anon = run.call(question, page, anonymous=True)
                except _Stop as stop:
                    out.stop_reason = str(stop)
                    return out
                except Exception:  # noqa: BLE001
                    anon = None
                if _records(anon):
                    run.cookie_status = "expired"
                    logger.warning("wencai cookie: expired (length=%d); continuing anonymously",
                                   run.cookie_length)
                    run.cookie = None
                    result, failure = anon, None
            rows = _records(result) if failure is None else []
            if failure is None:
                out.total = _page_total(result, out.total)
            expected = _expected_pages(out.total, run.page_size)
            truncated = False
            if failure is None:
                if not rows:
                    truncated = expected is not None and (page > 1 or bool(out.total))
                elif expected is not None and page < expected and len(rows) < run.page_size:
                    truncated = True
            forbidden = failure is not None and bool(_FORBIDDEN.search(str(failure)))
            if (failure is not None or truncated) and (page > 1 or not first_unit) and not forbidden \
                    and attempt < run.max_cooldowns:
                try:
                    run.cooldown(attempt)
                except _Stop as stop:
                    out.stop_reason = str(stop)
                    out.error, out.truncated = failure, truncated
                    if rows:
                        out.pages[page] = rows
                    return out
                attempt += 1
                continue
            if failure is not None:
                if page == 1 and not out.pages:
                    raise failure
                out.error = failure
                return out
            out.truncated = truncated
            if rows:
                out.pages[page] = rows
                if not truncated:
                    run.remember(key, {"rows": rows, "total": out.total})
            break
        if out.truncated or not rows:
            return out
        if page == 1 and stop_if_total_over is not None and out.total is not None \
                and out.total > stop_if_total_over:
            out.oversize = True
            return out
        expected = _expected_pages(out.total, run.page_size)
        if (expected is not None and page >= expected) or (expected is None and len(rows) < run.page_size):
            break
        page += 1
        if page % 8 == 0:
            run.save_checkpoint()
    return out


def _segment_question(base: str, lo: float | None, hi: float | None) -> str:
    parts = [base]
    if lo is not None and lo > 0:
        parts.append(f"流通市值大于{lo:g}亿")
    if hi is not None:
        parts.append(f"流通市值小于{hi:g}亿")
    return ";".join(parts)


def _dates_match(rows: list[dict[str, Any]], trade_date: str) -> bool:
    source_dates, values = _source_dates(rows)
    return values == [trade_date] and all(source_dates.get(f) == trade_date for f in _DATE_FIELDS)


def _fetch_segments(run: _Run, base: str, *, trade_date: str, edges: tuple[float | None, ...],
                    max_rows: int, min_width: float, max_pages: int
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Market-cap ranges [lo, hi) fetched independently, each with its own paging, row-count check and
    column-date check. An oversize or still-incomplete range is split in half (its partial rows stay and
    are de-duplicated by the caller)."""
    pending = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    rows: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    stop: str | None = None
    while pending:
        lo, hi = pending.pop(0)
        entry: dict[str, Any] = {"lo_yi": lo, "hi_yi": hi}
        report.append(entry)
        try:
            seg = _fetch_query(run, _segment_question(base, lo, hi), unit_prefix=f"s[{lo},{hi}]",
                               max_pages=max_pages, stop_if_total_over=max_rows)
        except Exception as exc:  # noqa: BLE001
            entry.update(ok=False, rows=0, reason=f"fetch_failed:{type(exc).__name__}")
            continue
        if seg.oversize and not (lo is not None and hi is not None and (hi - lo) > min_width):
            # cannot be split (open-ended or too narrow): oversize is only a hint, so page it fully
            # (page 1 comes from the checkpoint cache, not from a second request)
            seg = _fetch_query(run, _segment_question(base, lo, hi), unit_prefix=f"s[{lo},{hi}]",
                               max_pages=max_pages)
        seg_rows = seg.rows
        entry.update(rows=len(seg_rows), total=seg.total)
        if seg.stop_reason:
            entry.update(ok=False, reason=seg.stop_reason)
            rows.extend(seg_rows)
            stop = seg.stop_reason
            break
        if seg.oversize or not seg.complete:
            rows.extend(seg_rows)
            if lo is not None and hi is not None and (hi - lo) > min_width:
                mid = (lo + hi) / 2
                entry.update(ok=False, split=True, reason="oversize" if seg.oversize else "incomplete")
                pending[0:0] = [(lo, mid), (mid, hi)]
            else:
                entry.update(ok=False, reason="oversize_unsplittable" if seg.oversize else "incomplete")
            continue
        if seg_rows and not _dates_match(seg_rows, trade_date):
            entry.update(ok=False, rows=0, reason="date_mismatch")
            continue
        entry.update(ok=True)
        rows.extend(seg_rows)
        run.save_checkpoint()
    return rows, report, stop


def fetch_daily_kline(trade_date: str, context: Any = None, *,
                      query_fn: Callable[..., Any] | None = None,
                      page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES,
                      cookie_file: str | os.PathLike[str] | None = None,
                      cooldown_seconds: float = 60.0, cooldown_factor: float = 2.0,
                      cooldown_max_seconds: float = 300.0, max_cooldowns_per_page: int = 3,
                      max_total_requests: int = 150, wall_budget_seconds: float | None = None,
                      segment_enabled: bool = True,
                      segment_edges_yi: tuple[float | None, ...] = DEFAULT_SEGMENT_EDGES_YI,
                      segment_max_rows: int = 1500, segment_min_width_yi: float = 1.0,
                      universe_symbols: Iterable[str] | None = None, coverage_threshold: float = 0.95,
                      checkpoint_path: str | os.PathLike[str] | None = None,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      clock_fn: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Fetch one trade date: paged query with same-page cooldown resume, then market-cap segments.

    ``cookie_file`` (optional) is read here only; its content goes to the upstream request and is never
    logged, returned or stored. A missing file means anonymous access.
    """
    if page_size < 1 or max_pages < 1:
        raise ValueError("invalid_wencai_pagination_bounds")
    if cooldown_seconds < 0 or cooldown_factor < 1 or max_cooldowns_per_page < 0 or max_total_requests < 1:
        raise ValueError("invalid_wencai_throttle_bounds")
    edges = tuple(segment_edges_yi)
    finite = [e for e in edges if e is not None]
    if len(edges) < 2 or edges[0] != 0 or finite != sorted(set(finite)) or None in edges[:-1]:
        raise ValueError("invalid_wencai_segment_edges")
    if query_fn is None:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
    question = build_daily_kline_query(trade_date)
    cookie = _read_cookie(cookie_file)
    run = _Run(query_fn=query_fn, context=context, page_size=page_size, cookie=cookie,
               cooldown_seconds=float(cooldown_seconds), cooldown_factor=float(cooldown_factor),
               cooldown_max_seconds=float(cooldown_max_seconds), max_cooldowns=int(max_cooldowns_per_page),
               max_requests=int(max_total_requests), sleep_fn=sleep_fn, clock_fn=clock_fn,
               wall_budget_seconds=wall_budget_seconds, checkpoint_path=checkpoint_path,
               trade_date=trade_date, question=question)
    universe = {str(s).zfill(6) for s in universe_symbols} if universe_symbols is not None else None

    primary = _fetch_query(run, question, unit_prefix="p", max_pages=max_pages, first_unit=True)
    logger.info("wencai cookie: %s (length=%d)", run.cookie_status, run.cookie_length)
    rows = primary.rows
    total_count = primary.total
    stop_reason = primary.stop_reason
    page_error = primary.error
    sufficient = primary.complete
    fetch_mode = "paged"
    segments: list[dict[str, Any]] = []
    duplicate_rows = 0

    def coverage_of(items: list[dict[str, Any]]) -> float | None:
        if universe is None:
            return None
        if not universe:
            return 1.0
        return len({_symbol_of_raw(r) for r in items} & universe) / len(universe)

    blocked = page_error is not None and bool(_FORBIDDEN.search(str(page_error)))
    if not sufficient and segment_enabled and not blocked and stop_reason is None and rows:
        fetch_mode = "segmented"
        seg_rows, segments, seg_stop = _fetch_segments(
            run, question, trade_date=trade_date, edges=edges, max_rows=int(segment_max_rows),
            min_width=float(segment_min_width_yi), max_pages=max_pages)
        rows, duplicate_rows = dedupe_rows(list(rows) + seg_rows, trade_date)
        stop_reason = seg_stop
        if universe is not None:
            sufficient = (coverage_of(rows) or 0.0) >= coverage_threshold
        else:
            sufficient = (total_count is not None and len(rows) >= coverage_threshold * total_count
                          and all(s.get("ok") or s.get("split") for s in segments))
        if sufficient:
            page_error = None
    run.save_checkpoint(force=not sufficient)
    if sufficient:
        run.clear_checkpoint()

    source_dates, source_date_values = _source_dates(rows)
    payload: dict[str, Any] = {
        "rows": rows,
        "expected_count": None,
        "page_count": len(primary.pages),
        "query": question,
        "source_dates": source_dates,
        "source_date_values": source_date_values,
        "source_dates_consistent": (bool(source_date_values) and len(source_date_values) == 1 and
                                    all(source_dates.get(field) is not None for field in _DATE_FIELDS)),
        "field_units": _source_units(rows),
        "fetch_mode": fetch_mode,
        "cookie_status": run.cookie_status,
        "cookie_length": run.cookie_length,
        "request_count": run.requests,
        "cooldown_count": run.cooldowns,
        "duplicate_rows": duplicate_rows,
    }
    if segments:
        payload["segments"] = segments
    if total_count is not None:
        payload["expected_count"] = total_count
        payload["source_row_count"] = total_count
        payload["partial"] = not sufficient
    if fetch_mode == "segmented":
        cov = coverage_of(rows)
        if cov is not None:
            payload["universe_coverage"] = round(cov, 6)
            payload["expected_count"] = len(universe or ())
    if not sufficient and rows:
        timed_out = page_error is not None and (isinstance(page_error, TimeoutError) or re.search(
            r"TIMEOUT|TIMED OUT|READ TIMEOUT", str(page_error), re.I))
        reason = (f"page_fetch_failed:{type(page_error).__name__}" if page_error is not None else
                  stop_reason or ("segments_insufficient" if fetch_mode == "segmented" else "pages_truncated"))
        payload.update({"partial": True,
                        "error_code": "UPSTREAM_TIMEOUT" if timed_out else "TIER_PARTIAL",
                        "retryable": True, "reason": _redact(reason, cookie)})
    if not rows:
        payload.update({"status": "TIER_NO_DATA", "error_code": "TIER_NO_DATA",
                        "retryable": False, "reason": "empty_response"})
        if primary.truncated or stop_reason:
            payload.update({"status": "PROVIDER_UNAVAILABLE", "partial": True,
                            "error_code": "TIER_PARTIAL", "retryable": True,
                            "reason": stop_reason or "pages_truncated"})
    return payload
