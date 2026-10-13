"""Fetch raw Kaipan ban-pool and reason responses into an offline snapshot."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote


DEFAULT_LEGACY_ROOT = "/home/exedev/repos/stock-situation-runtime"
DEFAULT_DATES = "2026-10-09,2026-10-12"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _write_manifest(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
    temporary.replace(path)


def _make_reason_fetcher(analyzer: Any, page_size: int) -> Callable[[str, int], Any]:
    if page_size == 1:
        return lambda code, _page_size: analyzer.get_ban_reasons(code)

    def fetch(code: str, requested_page_size: int) -> Any:
        # Import at call time so the offline/injected path does not load legacy SDK code.
        kaipan = importlib.import_module("pykaipan.pykaipan")
        config = kaipan.config
        # Same envelope as pykaipan.getBanReasons; only st (page size) differs.
        query = (
            f"a=GetDayZhangTing&st={int(requested_page_size)}&c=HisLimitResumption&PhoneOSNew=1"
            f"&DeviceID={config['DeviceID']}&VerSion=5.19.0.0&Token={config['Token']}"
            f"&Index=0&apiv=w40&StockID={code}&UserID={config['UserID']}&"
        )
        return kaipan.reques("https://apphis.longhuvip.com/w1/api/index.php?" + query)

    return fetch


def _pool_code(value: Any) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    match = re.search(r"(\d{6})$", str(value).strip())
    return match.group(1) if match else None


def _pool_records(value: Any) -> list[list[Any] | tuple[Any, ...]]:
    if not isinstance(value, dict) or not isinstance(value.get("info"), (list, tuple)):
        return []
    records: list[list[Any] | tuple[Any, ...]] = []
    for page in value["info"]:
        if not isinstance(page, (list, tuple)):
            continue
        for row in page:
            if (isinstance(row, (list, tuple)) and len(row) >= 16
                    and _pool_code(row[0]) is not None):
                records.append(row)
    return records


def _valid_pool_shape(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("info"), (list, tuple)):
        return False
    for page in value["info"]:
        if not isinstance(page, (list, tuple)):
            continue
        if not page or any(
            isinstance(row, Mapping) or
            (isinstance(row, (list, tuple)) and len(row) >= 16 and _pool_code(row[0]) is not None)
            for row in page
        ):
            return True
    return False


def _redact_response(value: Any, secrets: set[str]) -> tuple[Any, int]:
    if isinstance(value, str):
        return ("<redacted>", 1) if value in secrets else (value, 0)
    if isinstance(value, Mapping):
        cleaned, count = {}, 0
        for key, child in value.items():
            cleaned_child, child_count = _redact_response(child, secrets)
            cleaned[key] = cleaned_child
            count += child_count
        return cleaned, count
    if isinstance(value, (list, tuple)):
        cleaned, count = [], 0
        for child in value:
            cleaned_child, child_count = _redact_response(child, secrets)
            cleaned.append(cleaned_child)
            count += child_count
        return cleaned, count
    return value, 0


def _runtime_credentials(config: Mapping[str, Any] | None) -> set[str]:
    if config is None:
        try:
            config = importlib.import_module("pykaipan.pykaipan").config
        except (ImportError, AttributeError):
            return set()
    return {value for key in ("Token", "DeviceID")
            if isinstance((value := config.get(key)), str) and value}


def _response_rows(value: Any) -> int:
    return len(_pool_records(value))


def _hot_plate_rows(value: Any) -> int | None:
    if isinstance(value, Mapping):
        for key in ("list", "List", "data", "info"):
            rows = value.get(key)
            if isinstance(rows, (list, tuple)):
                return len(rows)
        return None
    return None


def _pool_symbols(value: Any) -> Iterable[str]:
    for row in _pool_records(value):
        symbol = _pool_code(row[0])
        if symbol is not None:
            yield symbol


def _filename_symbol(symbol: str) -> str:
    return quote(symbol, safe="")


def fetch_all(
    analyzer: Any,
    dates: list[str],
    *,
    output_dir: str | Path,
    max_ban: int = 5,
    size: int = 200,
    sleep_s: float = 0.7,
    max_requests: int = 400,
    reason_symbols_from_pool: bool = True,
    extra_symbols: Iterable[str] = (),
    reason_page_size: int = 1,
    hot_plate_dates: list[str] | None = None,
    reason_fetcher: Callable[[str, int], Any] | None = None,
    credential_config: Mapping[str, Any] | None = None,
    manifest_every: int = 50,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """Fetch responses through the injected legacy analyzer and write raw JSON."""
    target = Path(output_dir)
    extra_symbols = tuple(str(value).strip() for value in extra_symbols if str(value).strip())
    hot_plate_dates = list(hot_plate_dates or [])
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError("output directory must not exist or must be empty")
    if max_requests < 0:
        raise ValueError("max_requests must be non-negative")
    if reason_page_size <= 0:
        raise ValueError("reason_page_size must be positive")
    if manifest_every <= 0:
        raise ValueError("manifest_every must be positive")
    if target.exists():
        target.rmdir()
    target.mkdir(parents=True)
    pools_dir = target / "pools"
    reasons_dir = target / "reasons"
    hot_plates_dir = target / "hot_plates"
    pools_dir.mkdir()
    reasons_dir.mkdir()
    if hot_plate_dates:
        hot_plates_dir.mkdir()

    started_at = now_fn()
    request_count = 0
    consecutive_bad = 0
    truncated = False
    aborted = False
    failures: list[dict[str, Any]] = []
    possibly_truncated_symbols: list[str] = []
    checksums: dict[str, str] = {}
    redacted_response_count = 0
    credentials = _runtime_credentials(credential_config)
    pool_counts: dict[str, dict[str, int]] = {date: {} for date in dates}
    hot_plate_counts: dict[str, int | None] = {date: None for date in hot_plate_dates}
    pool_symbol_sets: dict[str, set[str]] = {date: set() for date in dates}
    symbols: list[str] = []
    seen_symbols: set[str] = set()
    if reason_fetcher is None:
        reason_fetcher = _make_reason_fetcher(analyzer, reason_page_size)

    def build_manifest(ended_at: str | None = None) -> dict[str, Any]:
        pool_complete_by_date = {
            trade_date: {"1", "2", "3", "4", "5"} <= set(pool_counts.get(trade_date, {}))
            for trade_date in dates
        }
        complete = (not failures and not truncated and not aborted
                    and all(pool_complete_by_date.values()))
        return {
            "parameters": {
                "dates": dates, "max_ban": max_ban, "size": size,
                "sleep_s": sleep_s, "max_requests": max_requests,
                "reason_symbols_from_pool": reason_symbols_from_pool,
                "extra_symbols": list(extra_symbols),
                "reason_page_size": reason_page_size,
                "hot_plate_dates": hot_plate_dates,
                "manifest_every": manifest_every,
            },
            "request_count": request_count,
            "pool_counts": pool_counts,
            "pool_complete_by_date": pool_complete_by_date,
            "complete": complete,
            "hot_plate_counts": hot_plate_counts,
            "pool_symbol_counts": {date: len(values) for date, values in pool_symbol_sets.items()},
            "failures": failures,
            "possibly_truncated_symbols": possibly_truncated_symbols,
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "sha256": checksums,
            "redacted_response_count": redacted_response_count,
            "truncated": truncated,
            "aborted": aborted,
        }

    def request(kind: str, *, date: str | None = None, lvl: int | None = None,
                symbol: str | None = None) -> bool:
        nonlocal request_count, consecutive_bad, truncated, aborted, redacted_response_count
        if request_count >= max_requests:
            truncated = True
            return False
        request_count += 1
        try:
            if kind == "pool":
                response = analyzer._call_api("getHisBans", date=date, ban=str(lvl), size=size)
            elif kind == "hot_plate":
                response = analyzer.get_his_plates(date)
            else:
                response = reason_fetcher(symbol or "", reason_page_size)
        except Exception as exc:
            entry: dict[str, Any] = {"error_type": type(exc).__name__}
            if date is not None:
                entry["date"] = date
            if lvl is not None:
                entry["lvl"] = lvl
            if symbol is not None:
                entry["symbol"] = symbol
            failures.append(entry)
            consecutive_bad += 1
            response = None
        else:
            response, redacted = _redact_response(response, credentials)
            redacted_response_count += redacted
            pool_shape_error = kind == "pool" and not _valid_pool_shape(response)
            if (pool_shape_error or response is None or response == "" or response == []
                    or response == () or response == {}):
                entry = {"error_type": "UnexpectedShape" if pool_shape_error else "EmptyResponse"}
                if date is not None:
                    entry["date"] = date
                if lvl is not None:
                    entry["lvl"] = lvl
                if symbol is not None:
                    entry["symbol"] = symbol
                failures.append(entry)
                consecutive_bad += 1
                response = None
            else:
                consecutive_bad = 0
        if response is not None:
            if kind == "pool":
                relative = f"pools/{date}_ban{lvl}.json"
                for found in _pool_symbols(response):
                    pool_symbol_sets[date].add(found)
                    if reason_symbols_from_pool:
                        if found not in seen_symbols:
                            seen_symbols.add(found)
                            symbols.append(found)
                pool_counts[date][str(lvl)] = _response_rows(response)
            elif kind == "hot_plate":
                relative = f"hot_plates/{date}.json"
                hot_plate_counts[date] = _hot_plate_rows(response)
            else:
                relative = f"reasons/{_filename_symbol(symbol or '')}.json"
                rows = response.get("List") if isinstance(response, dict) else None
                if isinstance(rows, list) and len(rows) == reason_page_size:
                    possibly_truncated_symbols.append(symbol or "")
            checksums[relative] = _write_json(target / relative, response)
        sleep_fn(sleep_s)
        if request_count % manifest_every == 0:
            _write_manifest(target / "manifest.json", build_manifest())
        if consecutive_bad >= 8:
            aborted = True
        return not aborted

    for date in dates:
        for lvl in range(1, max_ban + 1):
            if not request("pool", date=date, lvl=lvl):
                break
        if truncated or aborted:
            break

    if reason_symbols_from_pool and not truncated and not aborted:
        for symbol in extra_symbols:
            value = str(symbol).strip()
            if value and value not in seen_symbols:
                seen_symbols.add(value)
                symbols.append(value)
    elif not reason_symbols_from_pool:
        symbols.extend(value.strip() for value in extra_symbols if str(value).strip())

    if not truncated and not aborted:
        for symbol in dict.fromkeys(symbols):
            if not request("reason", symbol=symbol):
                break
            if aborted:
                break

    if not truncated and not aborted:
        for date in hot_plate_dates:
            if not request("hot_plate", date=date):
                break
            if aborted:
                break

    manifest = build_manifest(ended_at=now_fn())
    _write_manifest(target / "manifest.json", manifest)
    return manifest


def _parse_dates(value: str, *, allow_empty: bool = True) -> list[str]:
    dates = [part.strip() for part in value.split(",") if part.strip()]
    if (not dates and not allow_empty) or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) for date in dates):
        raise argparse.ArgumentTypeError("dates must be comma-separated YYYY-MM-DD values")
    return dates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--dates", type=_parse_dates, default=_parse_dates(DEFAULT_DATES, allow_empty=False))
    parser.add_argument("--hot-plate-dates", type=_parse_dates, default=[])
    parser.add_argument("--max-ban", type=int, default=5)
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--sleep-s", type=float, default=0.7)
    parser.add_argument("--max-requests", type=int, default=400)
    parser.add_argument("--reason-page-size", type=int, default=1)
    parser.add_argument("--manifest-every", type=int, default=50)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reason-symbols-from-pool", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--extra-symbols", default="")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(args.legacy_root)))
    from ai.API.StockAnalyzer import StockAnalyzer  # type: ignore[import-not-found]

    manifest = fetch_all(
        StockAnalyzer(), args.dates, output_dir=args.output_dir,
        max_ban=args.max_ban, size=args.size, sleep_s=args.sleep_s,
        max_requests=args.max_requests,
        reason_page_size=args.reason_page_size,
        hot_plate_dates=args.hot_plate_dates,
        manifest_every=args.manifest_every,
        reason_symbols_from_pool=args.reason_symbols_from_pool,
        extra_symbols=[item.strip() for item in args.extra_symbols.split(",") if item.strip()],
    )
    print(f"requests={manifest['request_count']} truncated={manifest['truncated']} aborted={manifest['aborted']} "
          f"complete={manifest['complete']}")
    return 0 if manifest["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
