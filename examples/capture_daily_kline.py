"""Fetch one Baostock daily bar per symbol into a new local capture directory."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from engine_core.daily_kline_capture import BaostockClient, fetch_day, validate, write_capture


class _SleepClient:
    def __init__(self, client: Any, sleep_ms: int):
        self.client = client
        self.sleep_ms = sleep_ms

    def login(self):
        return self.client.login()

    def query(self, code: str, date: str):
        result = self.client.query(code, date)
        if self.sleep_ms:
            time.sleep(self.sleep_ms / 1000.0)
        return result

    def logout(self):
        return self.client.logout()


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbols-json", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--prev-close-json")
    parser.add_argument("--max-errors", type=int, default=50)
    parser.add_argument("--sleep-ms", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        if args.sleep_ms < 0:
            raise ValueError("--sleep-ms must be non-negative")
        symbols_value = _load_json(args.symbols_json)
        symbols = symbols_value.get("symbols") if isinstance(symbols_value, dict) else symbols_value
        if not isinstance(symbols, list):
            raise ValueError("symbols JSON must be a list or an object with a symbols list")
        prev_close = _load_json(args.prev_close_json) if args.prev_close_json else None
        if prev_close is not None and not isinstance(prev_close, dict):
            raise ValueError("previous close JSON must be a symbol-to-price object")
        if client is None:
            try:
                import baostock as bs
            except ImportError as exc:
                raise RuntimeError("Baostock library is required for live capture") from exc
            client = BaostockClient(bs)
        rows, fetch_report = fetch_day(_SleepClient(client, args.sleep_ms), symbols, args.date, max_errors=args.max_errors)
        quality = validate(rows, prev_close_by_symbol=prev_close)
        manifest_meta = {
            "date": args.date, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "symbol_count": len(symbols),
            "valid": quality["valid"], "suspended": quality["suspended"],
            "missing": len(fetch_report["missing"]) + quality["missing"],
            "error_count": len(fetch_report["errors"]),
            "mismatch_count": len(quality["mismatches"]) + len(quality["pct_chg_mismatches"]),
            "partial": fetch_report["partial"],
        }
        manifest = write_capture(args.out_dir, rows, manifest_meta)
        print(json.dumps({"manifest": manifest, "missing_symbols": fetch_report["missing"],
                          "errors": fetch_report["errors"], "mismatches": quality["mismatches"],
                          "pct_chg_mismatches": quality["pct_chg_mismatches"]}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        parser.exit(2, f"capture_daily_kline: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
