"""Versioned, immutable compressed slices used by report builders."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "DailyHistoryCompactV1"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize(rows: Sequence[Mapping[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    compact = []
    for row in rows:
        day = str(row.get("date") or row.get("ts") or "")[:10]
        symbol = str(row.get("symbol") or row.get("code") or "").strip().zfill(6)
        if not day or not symbol or day >= trade_date:
            continue
        parsed: dict[str, Any] = {"date": day, "symbol": symbol}
        for key in ("open", "high", "low", "close", "pct_chg", "amount_yuan"):
            value = row.get(key)
            try:
                parsed[key] = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                parsed[key] = None
        compact.append(parsed)
    compact.sort(key=lambda row: (row["symbol"], row["date"]))
    return compact


def build_daily_history_slice(rows: Sequence[Mapping[str, Any]], *, trade_date: str,
                              source_sha256: str, asof: str,
                              source_path: str | None = None) -> dict[str, Any]:
    compact = _normalize(rows, trade_date)
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in compact:
        by_symbol.setdefault(row["symbol"], []).append(row)
    derived = {}
    for symbol, history in by_symbol.items():
        recent = history[-64:]
        closes = [row["close"] for row in recent if row["close"] is not None]
        derived[symbol] = {
            "last_date": history[-1]["date"],
            "prior_close": history[-1]["close"],
            "range20_low": min(closes[-20:]) if len(closes) >= 20 else None,
            "range20_high": max(closes[-20:]) if len(closes) >= 20 else None,
        }
    return {"schema": SCHEMA, "trade_date": trade_date, "asof": asof,
            "source_sha256": source_sha256, "source_path": source_path,
            "row_count": len(compact), "symbol_count": len(by_symbol),
            "rows": compact, "by_symbol": derived}


def write_daily_history_slice(root: str | Path, trade_date: str,
                              slice_data: Mapping[str, Any]) -> Path:
    directory = Path(root) / trade_date
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / "daily_history_compact.json.gz"
    temporary = data_path.with_suffix(data_path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(slice_data, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, data_path)
    manifest = {key: slice_data.get(key) for key in
                ("schema", "trade_date", "asof", "source_sha256", "source_path", "row_count", "symbol_count")}
    manifest["data_sha256"] = sha256_file(data_path)
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
                              encoding="utf-8")
    return data_path


def load_daily_history_slice(root: str | Path, trade_date: str) -> tuple[list[dict[str, Any]] | None,
                                                                          dict[str, Any] | None]:
    directory = Path(root) / trade_date
    manifest_path = directory / "manifest.json"
    data_path = directory / "daily_history_compact.json.gz"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != SCHEMA or manifest.get("trade_date") != trade_date:
            return None, None
        if sha256_file(data_path) != manifest.get("data_sha256"):
            return None, None
        with gzip.open(data_path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if (data.get("schema") != SCHEMA or data.get("trade_date") != trade_date
                or len(data.get("rows", [])) != manifest.get("row_count")
                or len(data.get("by_symbol", {})) != manifest.get("symbol_count")):
            return None, None
        return data["rows"], manifest
    except (OSError, ValueError, KeyError, TypeError, gzip.BadGzipFile):
        return None, None


def read_daily_history_csv(path: str | Path, trade_date: str) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
    return _normalize(rows, trade_date)
