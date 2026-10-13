"""Fetch raw Kaipan ban-pool and reason responses into an offline snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote


DEFAULT_LEGACY_ROOT = "/home/exedev/repos/stock-situation-runtime"
DEFAULT_DATES = "2026-10-09,2026-10-12"
_SYMBOL_KEYS = {"zscode", "code", "symbol", "stockcode"}
_ROW_KEYS = ("data", "rows", "items", "stocks", "list")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _response_rows(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in _ROW_KEYS:
            if key in value:
                nested = value[key]
                if isinstance(nested, (list, dict)):
                    return _response_rows(nested)
        return len(value) if value else 0
    return 0


def _pool_symbols(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from _pool_symbols(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _SYMBOL_KEYS and isinstance(item, (str, int)):
                symbol = str(item).strip()
                if symbol:
                    yield symbol
            elif isinstance(item, (dict, list)):
                yield from _pool_symbols(item)


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
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """Fetch responses through the injected legacy analyzer and write raw JSON."""
    target = Path(output_dir)
    extra_symbols = tuple(str(value).strip() for value in extra_symbols if str(value).strip())
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError("output directory must not exist or must be empty")
    if max_requests < 0:
        raise ValueError("max_requests must be non-negative")
    if target.exists():
        target.rmdir()
    target.mkdir(parents=True)
    pools_dir = target / "pools"
    reasons_dir = target / "reasons"
    pools_dir.mkdir()
    reasons_dir.mkdir()

    started_at = now_fn()
    request_count = 0
    consecutive_bad = 0
    truncated = False
    aborted = False
    failures: list[dict[str, Any]] = []
    checksums: dict[str, str] = {}
    pool_counts: dict[str, dict[str, int]] = {date: {} for date in dates}
    symbols: list[str] = []
    seen_symbols: set[str] = set()

    def request(kind: str, *, date: str | None = None, lvl: int | None = None,
                symbol: str | None = None) -> bool:
        nonlocal request_count, consecutive_bad, truncated, aborted
        if request_count >= max_requests:
            truncated = True
            return False
        request_count += 1
        try:
            if kind == "pool":
                response = analyzer._call_api("getHisBans", date=date, ban=str(lvl), size=size)
            else:
                response = analyzer.get_ban_reasons(symbol)
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
            if response is None or response == "" or response == [] or response == {}:
                entry = {"error_type": "EmptyResponse"}
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
                if reason_symbols_from_pool:
                    for found in _pool_symbols(response):
                        if found not in seen_symbols:
                            seen_symbols.add(found)
                            symbols.append(found)
                pool_counts[date][str(lvl)] = _response_rows(response)
            else:
                relative = f"reasons/{_filename_symbol(symbol or '')}.json"
            checksums[relative] = _write_json(target / relative, response)
        sleep_fn(sleep_s)
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

    manifest = {
        "parameters": {
            "dates": dates, "max_ban": max_ban, "size": size,
            "sleep_s": sleep_s, "max_requests": max_requests,
            "reason_symbols_from_pool": reason_symbols_from_pool,
        "extra_symbols": list(extra_symbols),
        },
        "request_count": request_count,
        "pool_counts": pool_counts,
        "failures": failures,
        "started_at_utc": started_at,
        "ended_at_utc": now_fn(),
        "sha256": checksums,
        "truncated": truncated,
        "aborted": aborted,
    }
    _write_json(target / "manifest.json", manifest)
    return manifest


def _parse_dates(value: str) -> list[str]:
    dates = [part.strip() for part in value.split(",") if part.strip()]
    if not dates or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) for date in dates):
        raise argparse.ArgumentTypeError("dates must be comma-separated YYYY-MM-DD values")
    return dates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--dates", type=_parse_dates, default=_parse_dates(DEFAULT_DATES))
    parser.add_argument("--max-ban", type=int, default=5)
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--sleep-s", type=float, default=0.7)
    parser.add_argument("--max-requests", type=int, default=400)
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
        reason_symbols_from_pool=args.reason_symbols_from_pool,
        extra_symbols=[item.strip() for item in args.extra_symbols.split(",") if item.strip()],
    )
    print(f"requests={manifest['request_count']} truncated={manifest['truncated']} aborted={manifest['aborted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
