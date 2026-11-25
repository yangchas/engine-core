"""On-demand WenCai fallback normalization; network access is injected by callers."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class FallbackResult:
    rows: list[dict[str, Any]]
    labels: list[str]
    query_log: list[dict[str, Any]]
    complete: bool
    requested_date: str | None
    as_of_date: str | None = None


_DATE_SUFFIX = re.compile(r"\[(\d{8})\]")
_CODE_KEYS = ("股票代码", "代码", "证券代码", "symbol")
_NAME_KEYS = ("股票简称", "股票名称", "名称", "证券简称", "name")


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict(orient="records")
            if isinstance(rows, list):
                return [dict(row) for row in rows]
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return [dict(value)]
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    raw = str(value).strip().replace(",", "")
    if not raw or raw in {"--", "-", "None", "nan", "NaN"}:
        return None
    raw = raw.removesuffix("%").strip()
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def _find(row: Mapping[str, Any], *hints: str) -> Any:
    for key in row:
        if any(hint.lower() == str(key).lower() or hint.lower() in str(key).lower() for hint in hints):
            return row[key]
    return None


def _symbol(row: Mapping[str, Any]) -> str | None:
    value = next((row[k] for k in _CODE_KEYS if k in row and row[k] is not None), None)
    if value is None:
        value = _find(row, "股票代码", "证券代码")
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(6) if digits and len(digits) <= 6 else (digits[-6:] if digits else None)


def _is_st(row: Mapping[str, Any], symbol: str | None, from_st_query: bool) -> bool:
    explicit = _find(row, "是否st", "是否ST", "st标记")
    if explicit is not None:
        if str(explicit).strip().lower() in {"1", "true", "是", "st"}:
            return True
        if str(explicit).strip().lower() in {"0", "false", "否", "非st"}:
            return False
    name = str(next((row[k] for k in _NAME_KEYS if k in row and row[k] is not None), ""))
    return from_st_query or "st" in name.lower() or "*st" in name.lower()


def _date_state(rows: list[dict[str, Any]], date: str | None) -> tuple[set[str], str | None, bool]:
    suffixes = {match.group(1) for row in rows for key in row for match in [_DATE_SUFFIX.search(str(key))] if match}
    as_of = next(iter(sorted(suffixes)), None)
    mismatch = date is not None and date.replace("-", "") not in suffixes
    return suffixes, f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}" if as_of else None, mismatch


def _query_rows(
    fetch: Callable[..., Any], queries: list[tuple[str, bool]], *, max_queries: int,
    sleep_s: float, sleeper: Callable[[float], None], cap: int,
) -> tuple[list[tuple[dict[str, Any], bool]], list[dict[str, Any]], bool]:
    got: list[tuple[dict[str, Any], bool]] = []
    log: list[dict[str, Any]] = []
    complete = True
    attempts = 0
    for query, from_st in queries:
        succeeded = False
        for retry in range(2):
            if attempts >= max_queries:
                complete = False
                log.append({"query": query, "status": "not_run", "error_type": None})
                break
            if retry:
                sleeper(sleep_s * 2)
            attempts += 1
            started = time.monotonic()
            try:
                value = fetch(query, True, cap)
            except Exception as exc:  # provider errors become explicit partial results
                log.append({"query": query, "status": "failed", "error_type": type(exc).__name__,
                            "elapsed_s": round(time.monotonic() - started, 6), "retry": retry})
                continue
            for row in _records(value):
                got.append((row, from_st))
            log.append({"query": query, "status": "ok", "error_type": None,
                        "row_count": len(_records(value)), "elapsed_s": round(time.monotonic() - started, 6),
                        "retry": retry})
            succeeded = True
            break
        if not succeeded:
            complete = False
    return got, log, complete


def _result_labels(*, complete: bool, date_mismatch: bool, empty: bool, wan: int | None = None,
                   auction: bool = False) -> list[str]:
    labels = ["WENCAI_FALLBACK"]
    if auction:
        labels.extend([f"COVERAGE_AMT_GE_{wan}W", "NOT_FULL_UNIVERSE"])
    else:
        labels.append("SEAL_EVIDENCE_FROM_WENCAI")
    if date_mismatch:
        labels.append("DATE_MISMATCH")
    if not complete:
        labels.append("PARTIAL_QUERY_FAILED")
    if empty:
        labels.append("EMPTY_RESULT")
    return labels


def fetch_auction_fallback(
    fetch: Callable[..., Any], *, date: str | None, min_amount_yuan: float = 5_000_000,
    include_st: bool = True, max_queries: int = 4, sleep_s: float = 4.0,
    sleeper: Callable[[float], None] = time.sleep, cap: int = 1000,
) -> FallbackResult:
    wan = int(min_amount_yuan / 10_000)
    prefix = f"{date[:4]}年{int(date[5:7])}月{int(date[8:10])}日" if date else "今日"
    queries = [(f"{prefix}集合竞价金额大于{wan}万;去除st", False)]
    if include_st:
        queries.append((f"{prefix}集合竞价金额大于{wan}万;st", True))
    pairs, query_log, complete = _query_rows(fetch, queries, max_queries=max_queries,
                                              sleep_s=sleep_s, sleeper=sleeper, cap=cap)
    raw = [row for row, _ in pairs]
    _, as_of_date, mismatch = _date_state(raw, date)
    by_symbol: dict[str, dict[str, Any]] = {}
    for row, from_st in pairs:
        symbol = _symbol(row)
        if not symbol:
            continue
        normalized = {
            "symbol": symbol,
            "name": next((row[k] for k in _NAME_KEYS if k in row), _find(row, "名称")),
            "amount_yuan": _number(_find(row, "竞价金额", "集合竞价金额", "竞价成交额")),
            "match_price": _number(_find(row, "竞价匹配价", "集合竞价匹配价")),
            "match_pct": _number(_find(row, "竞价涨幅", "集合竞价涨幅")),
            "match_volume": _number(_find(row, "竞价量", "竞价匹配量", "集合竞价量")),
            "unmatched_amount_yuan": _number(_find(row, "竞价未匹配金额", "未匹配金额")),
            "anomaly_type": _find(row, "竞价异动类型"),
            "is_st": _is_st(row, symbol, from_st),
            "is_bj": symbol.startswith(("920", "8", "4")),
            "source": "WENCAI",
        }
        old = by_symbol.get(symbol)
        if old is None or (old["is_st"] and not normalized["is_st"]):
            by_symbol[symbol] = normalized
    output = sorted(by_symbol.values(), key=lambda row: row["symbol"])
    return FallbackResult(output, _result_labels(complete=complete, date_mismatch=mismatch,
                                                  empty=not output, wan=wan, auction=True),
                          query_log, complete and not mismatch, date, as_of_date)


def fetch_limit_up_fallback(
    fetch: Callable[..., Any], *, date: str | None, include_st: bool = True,
    max_queries: int = 4, sleep_s: float = 4.0,
    sleeper: Callable[[float], None] = time.sleep, cap: int = 1000,
) -> FallbackResult:
    prefix = f"{date[:4]}年{int(date[5:7])}月{int(date[8:10])}日" if date else "今日"
    query = f"{prefix}涨停;连板天数" + ("" if include_st else ";去除st")
    pairs, query_log, complete = _query_rows(fetch, [(query, False)], max_queries=max_queries,
                                              sleep_s=sleep_s, sleeper=sleeper, cap=cap)
    raw = [row for row, _ in pairs]
    _, as_of_date, mismatch = _date_state(raw, date)
    output = []
    for row, _ in pairs:
        symbol = _symbol(row)
        if not symbol:
            continue
        seal_amount = _number(_find(row, "涨停封单额", "封单额"))
        output.append({
            "symbol": symbol,
            "name": next((row[k] for k in _NAME_KEYS if k in row), _find(row, "名称")),
            "lb_days": _number(_find(row, "连续涨停天数", "连板天数", "几天几板")),
            "first_limit_time": _find(row, "首次涨停时间"),
            "last_limit_time": _find(row, "最终涨停时间", "最后涨停时间"),
            "seal_qty": _number(_find(row, "涨停封单量", "封单量")),
            "seal_amount_yuan": seal_amount,
            "seal_to_volume_ratio": _number(_find(row, "涨停封单量占成交量比", "封单量占成交量比")),
            "seal_to_float_ratio": _number(_find(row, "涨停封单量占流通a股比", "封单量占流通A股比")),
            "open_times": _number(_find(row, "涨停开板次数", "开板次数")),
            "limit_type": _find(row, "涨停类型"),
            "reason_class": _find(row, "涨停原因类别"),
            "is_st": _is_st(row, symbol, False),
            "source": "WENCAI",
            "sealed": seal_amount > 0 if seal_amount is not None else None,
        })
    output.sort(key=lambda row: row["symbol"])
    return FallbackResult(output, _result_labels(complete=complete, date_mismatch=mismatch,
                                                  empty=not output), query_log,
                          complete and not mismatch, date, as_of_date)
