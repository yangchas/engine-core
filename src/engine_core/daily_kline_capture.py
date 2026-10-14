"""Read-only Baostock daily bar capture and local file loader."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BAOSTOCK_FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "turn", "pctChg",
)
_NUMERIC_FIELDS = {
    "open": ("open",), "high": ("high",), "low": ("low",), "close": ("close",),
    "pre_close": ("pre_close", "preclose", "preClose"),
    "volume": ("volume",), "amount_yuan": ("amount_yuan", "amount"),
    "turnover_pct": ("turnover_pct", "turn"), "pct_chg": ("pct_chg", "pctChg"),
}


def to_baostock_code(symbol6: str) -> str:
    symbol = str(symbol6)
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"symbol must be exactly six digits: {symbol6!r}")
    if symbol.startswith("92"):
        market = "bj"
    elif symbol[0] in "69":
        market = "sh"
    elif symbol[0] in "03":
        market = "sz"
    elif symbol[0] in "48":
        market = "bj"
    else:
        raise ValueError(f"unsupported symbol prefix: {symbol}")
    return f"{market}.{symbol}"


def _raw_mapping(raw: Mapping[str, Any] | Sequence[Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("raw row must be a mapping or sequence")
    return dict(zip(_BAOSTOCK_FIELDS, raw))


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _symbol(value: Any) -> str:
    symbol = str(value or "")
    if "." in symbol:
        symbol = symbol.rsplit(".", 1)[-1]
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"invalid symbol in daily row: {value!r}")
    return symbol


def parse_row(raw: Mapping[str, Any] | Sequence[Any]) -> dict[str, Any]:
    data = _raw_mapping(raw)
    code = data.get("symbol", data.get("code"))
    row: dict[str, Any] = {"symbol": _symbol(code), "date": str(data.get("date") or "")}
    for output_name, candidates in _NUMERIC_FIELDS.items():
        value = next((data[name] for name in candidates if name in data), None)
        row[output_name] = _as_float(value)
    volume, amount = row["volume"], row["amount_yuan"]
    row["suspended"] = (volume is None or volume == 0) and (amount is None or amount == 0)
    return row


def fetch_day(
    client: Any,
    symbols: Sequence[str],
    date: str,
    *,
    max_errors: int = 50,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_errors < 0:
        raise ValueError("max_errors must be non-negative")
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    missing: list[str] = []
    partial = False
    client.login()
    try:
        for raw_symbol in symbols:
            symbol = _symbol(raw_symbol)
            code = to_baostock_code(symbol)
            try:
                result = client.query(code, date)
                found = list(result or [])
                if not found:
                    missing.append(symbol)
                    continue
                for raw in found:
                    row = parse_row(raw)
                    if row["symbol"] != symbol or row["date"] != date:
                        raise ValueError(f"response identity mismatch for {symbol} on {date}")
                    rows.append(row)
            except Exception as exc:  # isolate a failed symbol from the remaining universe
                errors.append({"symbol": symbol, "code": code, "msg": str(exc)})
                if len(errors) > max_errors:
                    partial = True
                    break
    finally:
        client.logout()
    return rows, {"errors": errors, "missing": missing, "partial": partial}


def validate(
    rows: Sequence[Mapping[str, Any]],
    prev_close_by_symbol: Mapping[str, float] | None = None,
    tol: float = 0.0015,
) -> dict[str, Any]:
    valid = 0
    suspended = 0
    missing = 0
    mismatches: list[dict[str, Any]] = []
    pct_chg_mismatches: list[str] = []
    for row in rows:
        required = ("open", "high", "low", "close", "pre_close")
        if row.get("suspended"):
            suspended += 1
        elif all(row.get(field) is not None for field in required):
            valid += 1
        else:
            missing += 1
        symbol = str(row["symbol"])
        pre_close = row.get("pre_close")
        if prev_close_by_symbol is not None and pre_close is not None and symbol in prev_close_by_symbol:
            expected = float(prev_close_by_symbol[symbol])
            if expected != 0 and abs(pre_close / expected - 1.0) > tol:
                mismatches.append({"symbol": symbol, "field": "pre_close", "actual": pre_close, "expected": expected})
        close, pct_chg = row.get("close"), row.get("pct_chg")
        if close is not None and pre_close not in (None, 0) and pct_chg is not None:
            expected_pct = (close / pre_close - 1.0) * 100.0
            if abs(pct_chg - expected_pct) > 0.02:
                pct_chg_mismatches.append(symbol)
    return {
        "total": len(rows), "valid": valid, "suspended": suspended, "missing": missing,
        "mismatches": mismatches, "pct_chg_mismatches": pct_chg_mismatches,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_capture(out_dir: str | Path, rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> dict[str, Any]:
    folder = Path(out_dir)
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise FileExistsError(f"capture output must be a non-existing or empty directory: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    data_path = folder / "daily_kline.jsonl"
    with data_path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    manifest = {
        "date": meta.get("date"), "source": str(meta.get("source") or "baostock"),
        "fetched_at": meta.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        "symbol_count": int(meta.get("symbol_count", len(rows))), "valid": int(meta.get("valid", 0)),
        "suspended": int(meta.get("suspended", 0)), "missing": int(meta.get("missing", 0)),
        "error_count": int(meta.get("error_count", 0)), "mismatch_count": int(meta.get("mismatch_count", 0)),
        "partial": bool(meta.get("partial", False)), "sha256": _sha256(data_path),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_capture(directory: str | Path) -> dict[str, dict[str, Any]]:
    folder = Path(directory)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    data_path = folder / "daily_kline.jsonl"
    if _sha256(data_path) != manifest.get("sha256"):
        raise ValueError("daily_kline.jsonl sha256 does not match manifest")
    rows: dict[str, dict[str, Any]] = {}
    with data_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            symbol = _symbol(row.get("symbol"))
            if symbol in rows:
                raise ValueError(f"duplicate symbol in capture: {symbol}")
            rows[symbol] = row
    return rows


class BaostockClient:
    """Small adapter around Baostock; it performs reads only."""

    def __init__(self, module: Any):
        self.module = module

    def login(self) -> None:
        result = self.module.login()
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(f"Baostock login failed: {getattr(result, 'error_msg', '')}")

    def query(self, code: str, date: str) -> list[dict[str, str]]:
        result = self.module.query_history_k_data_plus(
            code, ",".join(_BAOSTOCK_FIELDS), start_date=date, end_date=date,
            frequency="d", adjustflag="3",
        )
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(getattr(result, "error_msg", "Baostock query failed"))
        rows = []
        while result.next():
            rows.append(dict(zip(_BAOSTOCK_FIELDS, result.get_row_data())))
        return rows

    def logout(self) -> None:
        self.module.logout()
