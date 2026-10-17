"""Point-in-time theme attribution for the previous session's limit-up pool."""
from __future__ import annotations

import json
import hashlib
import os
import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping, Sequence


_SPLIT = re.compile(r"[、,，/；;]+")
_CODE = re.compile(r"\d{6}")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
THEME_SNAPSHOT_CACHE_TTL_SECONDS = 7 * 86400
THEME_SNAPSHOT_CACHE_SCHEMA = "ThemeSnapshotCacheV3"


def _cache_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def theme_snapshot_version(inputs: Mapping[str, Any]) -> str:
    """Stable version for the cold inputs used to build a target-day theme view."""
    return "sha256-" + hashlib.sha256(_cache_json(inputs)).hexdigest()[:16]


def theme_snapshot_visibility(rows_by_input: Mapping[str, Sequence[Any]]) -> dict[str, list[dict[str, Any]]]:
    """Fingerprint row visibility boundaries without duplicating full slice payloads."""
    fields = ("available_at", "sampled_at", "captured_local", "capture_time",
              "event_time", "event_at", "Date")
    result: dict[str, list[dict[str, Any]]] = {}
    for name, rows in rows_by_input.items():
        visible = []
        for row in rows:
            if isinstance(row, Mapping):
                boundary = {key: row[key] for key in fields if row.get(key) not in (None, "")}
                visible.append(boundary)
        result[name] = sorted(visible, key=_cache_json)
    return result


def write_theme_snapshot_cache(redis: Any, trade_date: str, previous_trade_date: str,
                               snapshot: Mapping[str, Any], *, as_of: str,
                               version: str) -> bool:
    if redis is None or not callable(getattr(redis, "set", None)) or not version:
        return False
    try:
        payload = {"schema": THEME_SNAPSHOT_CACHE_SCHEMA, "trade_date": trade_date,
                   "previous_trade_date": previous_trade_date, "pit_date": previous_trade_date,
                   "as_of": as_of, "version": version, "snapshot": dict(snapshot)}
        key = f"core:cache:data:theme_snapshot:{trade_date}"
        redis.set(key, _cache_json(payload), ex=THEME_SNAPSHOT_CACHE_TTL_SECONDS)
        return True
    except Exception:
        return False


def read_theme_snapshot_cache(redis: Any, trade_date: str, previous_trade_date: str, *,
                              asof: str, version: str | None = None) -> dict[str, Any] | None:
    if redis is None or not callable(getattr(redis, "get", None)):
        return None
    try:
        raw = redis.get(f"core:cache:data:theme_snapshot:{trade_date}")
        payload = json.loads(raw) if raw else None
        cutoff, built = _parse_time(asof), _parse_time((payload or {}).get("as_of"))
        snapshot = (payload or {}).get("snapshot")
        if (not isinstance(payload, Mapping) or payload.get("schema") != THEME_SNAPSHOT_CACHE_SCHEMA
                or payload.get("trade_date") != trade_date
                or payload.get("previous_trade_date") != previous_trade_date
                or payload.get("pit_date") != previous_trade_date
                or not isinstance(payload.get("version"), str) or not payload.get("version")
                or (version is not None and payload.get("version") != version)
                or cutoff is None or built is None or built > cutoff or not isinstance(snapshot, dict)):
            return None
        return snapshot
    except Exception:
        return None


def _pool_rows(value: Any) -> list[Sequence[Any] | Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
        return [row for item in value for row in _pool_rows(item)]
    if not isinstance(value, Mapping):
        return []
    if value.get("symbol") or value.get("code"):
        return [value]
    info = value.get("info")
    if not isinstance(info, Sequence) or isinstance(info, (str, bytes)):
        return []
    rows = []
    for page in info:
        if isinstance(page, Sequence) and not isinstance(page, (str, bytes)):
            rows.extend(row for row in page if isinstance(row, (Mapping, list, tuple)))
    return rows


def _symbol(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-6:].zfill(6) if len(digits) >= 6 else ""


def _pool_values(row: Sequence[Any] | Mapping[str, Any]) -> tuple[str, str, list[str], str, str]:
    if isinstance(row, Mapping):
        symbol = _symbol(row.get("symbol", row.get("code")))
        label = str(row.get("plate") or row.get("theme_name") or row.get("plate_label") or "").strip()
        topics = row.get("themes", row.get("theme_text", label))
        code = str(row.get("plate_code", row.get("theme_code", row.get("theme_codes", ""))) or "").strip()
        tier = str(row.get("tier_label", row.get("SCLT", "")) or "").strip()
    elif len(row) >= 20:
        symbol = _symbol(row[0])
        # getHisBans row contract: idx5 topic label, idx12 comma-separated
        # topic names, idx15 board height, idx19 topic/index code.
        label = str(row[5] or "").strip()
        topics = row[12]
        code = str(row[19] or "").strip()
        tier = ""  # SCLT belongs to reason records, not the pool row contract.
    else:
        return "", "", [], "", ""
    if isinstance(topics, str):
        topic_names = [part.strip() for part in _SPLIT.split(topics) if part.strip()]
    elif isinstance(topics, Sequence):
        topic_names = [str(part).strip() for part in topics if str(part).strip()]
    else:
        topic_names = []
    if label and label not in topic_names:
        topic_names.insert(0, label)
    return symbol, label, list(dict.fromkeys(topic_names)), code, tier


def _reason_records(response: Any, symbol_hint: str) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes, Mapping)):
        return [record for item in response for record in _reason_records(item, symbol_hint)]
    if not isinstance(response, Mapping):
        return []
    symbol = _symbol(response.get("StockID", response.get("symbol", symbol_hint)))
    rows = response.get("List", response.get("list", []))
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [(symbol, row) for row in rows if isinstance(row, Mapping)]


def _names(codes: Sequence[str], plate_names: Mapping[str, str]) -> list[dict[str, str]]:
    result = []
    for code in codes:
        code = str(code).strip()
        if code:
            result.append({"code": code, "name": str(plate_names.get(code) or code)})
    return result


def _short_reason(value: Any) -> str:
    text = str(value or "").split(";", 1)[0].split("；", 1)[0].splitlines()[0].strip() if value else ""
    return text[:12]


def _static_candidates(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        return [{"name": raw}]
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, Sequence):
        return [item if isinstance(item, Mapping) else {"name": str(item)}
                for item in raw]
    return []


def _assignment(symbol: str, key: str, name: str, source: str, *, secondary: Sequence[dict[str, str]] = (),
                reason: str = "", tier: str = "") -> dict[str, Any]:
    return {"symbol": symbol, "theme_key": key or name or "NONE", "theme_name": name or "无题材",
            "theme_source": source, "reason_text_short": _short_reason(reason),
            "tier_label": tier, "secondary_themes": list(secondary)}


def build_limit_reason_themes(
    pool_responses: Any,
    reason_responses_by_symbol: Mapping[str, Any],
    *, expected_previous_date: str,
    plate_names: Mapping[str, str],
    static_themes_by_symbol: Mapping[str, Any] | None = None,
    lookback_sessions: int = 20,
    trading_dates: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Assign one primary theme to each pool symbol using only prior-session facts."""
    pool: dict[str, tuple[str, list[str], str, str]] = {}
    for row in _pool_rows(pool_responses):
        symbol, label, topics, code, tier = _pool_values(row)
        if symbol and symbol not in pool:
            pool[symbol] = (label, topics, code, tier)

    result: dict[str, dict[str, Any]] = {}
    reason_symbols = {_symbol(value) for value in reason_responses_by_symbol}
    all_symbols = set(pool) | reason_symbols | {_symbol(value) for value in (static_themes_by_symbol or {})}
    session_set: set[str] | None = None
    if trading_dates:
        sessions = sorted({str(day)[:10] for day in trading_dates
                           if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)[:10])
                           and str(day)[:10] <= expected_previous_date})[-max(1, lookback_sessions):]
        session_set = set(sessions)
    for symbol in sorted(value for value in all_symbols if value):
        label, topics, pool_code, pool_tier = pool.get(symbol, ("", [], "", ""))
        records = []
        for _record_symbol, row in _reason_records(reason_responses_by_symbol.get(symbol), symbol):
            event_date = str(row.get("Date", row.get("date", "")) or "")[:10]
            if event_date > expected_previous_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
                continue
            if str(row.get("Type", row.get("type", "0")) or "0") != "0":
                continue
            codes = row.get("ZSCode", row.get("zs_code", []))
            if isinstance(codes, str):
                codes = [codes]
            codes = [str(code).strip() for code in codes if str(code).strip()] if isinstance(codes, Sequence) else []
            records.append((event_date, row, codes))
        records.sort(key=lambda item: item[0], reverse=True)
        if session_set is not None:
            records = [record for record in records if record[0] in session_set]
        else:
            # Without an exchange calendar, only the exact prior session can be
            # asserted. Older records cannot establish a 20-trading-day window.
            records = [record for record in records if record[0] == expected_previous_date]
        latest = records[0] if records else None
        selected: dict[str, Any] | None = None
        if label or pool_code:
            topic_names = [topic for topic in topics if topic]
            name = str(plate_names.get(pool_code) or label or (topic_names[0] if topic_names else "无题材"))
            primary = _assignment(symbol, pool_code or name, name, "POOL_LABEL",
                                  secondary=[{"code": "", "name": topic} for topic in topic_names
                                             if topic != name], tier=pool_tier)
            if latest:
                primary["reason_text_short"] = _short_reason(latest[1].get("Reason", latest[1].get("reason")))
                primary["tier_label"] = str(latest[1].get("SCLT", latest[1].get("sclt", "")) or pool_tier)
                extra = _names(latest[2][1:], plate_names)
                known = {(item["code"], item["name"]) for item in primary["secondary_themes"]}
                primary["secondary_themes"].extend(item for item in extra
                                                    if (item["code"], item["name"]) not in known)
            selected = primary
        elif latest and latest[2]:
            codes = latest[2]
            selected = _assignment(symbol, codes[0], str(plate_names.get(codes[0]) or codes[0]),
                                   "REASON_LATEST" if latest[0] == expected_previous_date else "REASON_HISTORY",
                                   secondary=_names(codes[1:], plate_names),
                                   reason=latest[1].get("Reason", latest[1].get("reason")),
                                   tier=str(latest[1].get("SCLT", latest[1].get("sclt", "")) or ""))
        elif records:
            counts: Counter[str] = Counter(code for _day, _row, codes in records
                                           for code in codes)
            if counts:
                first_order = [code for _day, _row, codes in records for code in codes]
                chosen = min(counts, key=lambda code: (-counts[code], first_order.index(code)))
                selected = _assignment(symbol, chosen, str(plate_names.get(chosen) or chosen), "REASON_HISTORY",
                                       secondary=_names([code for code in dict.fromkeys(first_order) if code != chosen], plate_names),
                                       reason=records[0][1].get("Reason", records[0][1].get("reason")),
                                       tier=str(records[0][1].get("SCLT", records[0][1].get("sclt", "")) or ""))
        if selected is None:
            for candidate in _static_candidates((static_themes_by_symbol or {}).get(symbol)):
                name = str(candidate.get("name") or "").strip()
                try:
                    size = int(candidate.get("size"))
                except (TypeError, ValueError):
                    continue
                if (candidate.get("class", candidate.get("kind")) in {"THEME", "NICHE"}
                        and 8 <= size <= 150 and name and not candidate.get("generic", False)):
                    code = str(candidate.get("code") or "").strip()
                    selected = _assignment(symbol, code or name, name, "STATIC_FALLBACK")
                    break
        result[symbol] = selected or _assignment(symbol, "NONE", "无题材", "NONE")
        result[symbol]["history_status"] = "CALENDAR_OK" if trading_dates else "CALENDAR_MISSING"
    return result


def merge_theme_snapshot_static_fallback(
    snapshot: Mapping[str, Any], static_themes_by_symbol: Mapping[str, Any], *,
    expected_previous_date: str, plate_names: Mapping[str, str],
    trading_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Add report-universe static fallbacks to a cached cold-data snapshot."""
    assignments = {str(symbol): dict(row) for symbol, row in
                   (snapshot.get("assignments", {}) or {}).items()
                   if isinstance(row, Mapping)}
    # The prepared snapshot may have been built with an earlier registry
    # snapshot. Resolve code-backed display names with the same registry used
    # by the report's direct-compute path.
    for row in assignments.values():
        theme_key = str(row.get("theme_key") or "").strip()
        if theme_key in plate_names:
            row["theme_name"] = str(plate_names[theme_key])
        secondary = row.get("secondary_themes")
        if isinstance(secondary, list):
            row["secondary_themes"] = [
                {**item, "name": str(plate_names.get(str(item.get("code") or ""), item.get("name") or ""))}
                if isinstance(item, Mapping) else item
                for item in secondary
            ]
    static_assignments = build_limit_reason_themes(
        {}, {}, expected_previous_date=expected_previous_date,
        plate_names=plate_names, static_themes_by_symbol=static_themes_by_symbol,
        trading_dates=trading_dates)
    for symbol, row in static_assignments.items():
        if row.get("theme_source") == "STATIC_FALLBACK" and (
                symbol not in assignments or assignments[symbol].get("theme_source") == "NONE"):
            assignments[symbol] = row
    source_counts: Counter[str] = Counter(row.get("theme_source", "NONE")
                                          for row in assignments.values())
    result = dict(snapshot)
    result["assignments"] = assignments
    result["source_counts"] = dict(source_counts)
    counts_text = ", ".join(f"{key}={source_counts[key]}" for key in sorted(source_counts)) or "无记录"
    status = snapshot.get("status", "PARTIAL")
    result["basis"] = (("切片：昨日涨停池/历史原因" if status == "READY"
                        else "切片不完整；登记表回退")
                       + f"；原因窗口 {'按交易日历' if trading_dates else 'CALENDAR_MISSING，不满足20交易日窗口'}"
                       + f"；来源计数 {counts_text}")
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_SHANGHAI)
    except ValueError:
        return None


def _latest_run(root: Path, report_date: str, observed_at: str | None) -> Path | None:
    cutoff = _parse_time(observed_at)
    candidates = []
    for directory in root.glob(report_date + "-*") if root.is_dir() else ():
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ended = _parse_time(str(manifest.get("ended_at_utc") or manifest.get("finished_at_utc") or ""))
        if ended is None or (cutoff is not None and ended > cutoff):
            continue
        candidates.append((ended, directory))
    return max(candidates, default=(None, None), key=lambda item: (item[0], str(item[1])))[1]


def load_kaipan_theme_snapshot(
    root: str | Path | None,
    *, report_date: str,
    previous_trade_date: str,
    observed_at: str | None,
    plate_names: Mapping[str, str],
    static_themes_by_symbol: Mapping[str, Any] | None = None,
    pool_rows: Sequence[Mapping[str, Any]] | None = None,
    reason_rows: Sequence[Mapping[str, Any]] | None = None,
    hot_rows: Sequence[Mapping[str, Any]] | None = None,
    trading_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load only a completed same-day snapshot visible at the report cutoff."""
    # No implicit validation/Kaipan directory input. Callers may pass an explicit
    # replay directory; production obtains these rows through the slice layer.
    if root is None:
        if pool_rows or reason_rows or hot_rows:
            reason_map: dict[str, dict[str, Any]] = {}
            for row in reason_rows or ():
                symbol = _symbol(row.get("symbol", row.get("code")))
                if not symbol:
                    continue
                record = {key: row.get(key) for key in
                          ("ZSCode", "PZSCode", "Reason", "Date", "SCLT", "GNSM", "Type")}
                reason_map.setdefault(symbol, {"StockID": symbol, "List": []})["List"].append(record)
            assignments = build_limit_reason_themes(
                list(pool_rows or ()), reason_map, expected_previous_date=previous_trade_date,
                plate_names=plate_names, static_themes_by_symbol=static_themes_by_symbol,
                lookback_sessions=20, trading_dates=trading_dates)
            cutoff = _parse_time(observed_at)
            latest_hot: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
            for row in hot_rows or ():
                code = str(row.get("code") or row.get("plate_code") or "").strip()
                sampled = _parse_time(str(row.get("sampled_at") or row.get("available_at") or ""))
                if not code or sampled is None or (cutoff is not None and sampled > cutoff):
                    continue
                if code not in latest_hot or sampled > latest_hot[code][0]:
                    latest_hot[code] = (sampled, row)
            hot_samples = {code: {"name": str(row.get("name") or row.get("plate_name") or ""),
                                  "sampled_at": str(row.get("sampled_at") or row.get("available_at") or "")}
                           for code, (_sampled, row) in latest_hot.items()}
            hot_names = sorted({item["name"] for item in hot_samples.values()} - {""})
            source_counts: Counter[str] = Counter(row.get("theme_source", "NONE") for row in assignments.values())
            status = "READY" if pool_rows and reason_rows else "PARTIAL"
            counts_text = ", ".join(f"{key}={source_counts[key]}" for key in sorted(source_counts)) or "无记录"
            return {"status": status, "assignments": assignments,
                    "basis": ("切片：昨日涨停池/历史原因" if status == "READY" else "切片不完整；登记表回退")
                    + f"；原因窗口 {'按交易日历' if trading_dates else 'CALENDAR_MISSING，不满足20交易日窗口'}；来源计数 {counts_text}",
                    "snapshot_dir": None, "hot_plate_names": hot_names,
                    "hot_plate_samples": hot_samples, "source_counts": dict(source_counts)}
        return {"status": "MISSING", "assignments": {}, "hot_plate_names": [],
                "hot_plate_samples": {}, "source_counts": {},
                "basis": "缺历史原因切片；登记表回退；来源计数 NONE=0", "snapshot_dir": None,
                "reason": "limit_reason_history_slice_unavailable"}
    base = Path(root)
    run = _latest_run(base, report_date, observed_at)
    status = "MISSING"
    assignments: dict[str, dict[str, Any]] = {}
    hot_names: list[str] = []
    if run:
        pool_paths = sorted((run / "pools").glob(f"{previous_trade_date}_ban*.json"))
        any_pool_paths = sorted((run / "pools").glob("*_ban*.json"))
        if not pool_paths and any_pool_paths:
            status = "STALE"
        elif pool_paths:
            try:
                manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                complete = (manifest.get("pool_complete_by_date", {}).get(previous_trade_date)
                            if isinstance(manifest.get("pool_complete_by_date"), Mapping) else None)
                ban_levels = {path.stem.rsplit("_ban", 1)[-1] for path in pool_paths}
                if complete is not True or ban_levels != {"1", "2", "3", "4", "5"}:
                    raise ValueError("incomplete_previous_pool")
                pool_responses = [json.loads(path.read_text(encoding="utf-8")) for path in pool_paths]
                reason_map = {}
                symbols = {_pool_values(row)[0] for response in pool_responses for row in _pool_rows(response)}
                for symbol in symbols - {""}:
                    reason_file = run / "reasons" / f"{symbol}.json"
                    if reason_file.is_file():
                        reason_map[symbol] = json.loads(reason_file.read_text(encoding="utf-8"))
                assignments = build_limit_reason_themes(
                    pool_responses, reason_map, expected_previous_date=previous_trade_date,
                    plate_names=plate_names, static_themes_by_symbol=static_themes_by_symbol)
                status = "READY"
            except (OSError, ValueError, TypeError):
                assignments = {}
                status = "MISSING"
        hot_path = run / "hot_plates" / f"{report_date}.json"
        if hot_path.is_file():
            try:
                hot = json.loads(hot_path.read_text(encoding="utf-8"))
                raw_rows = hot.get("list", hot.get("info", [])) if isinstance(hot, Mapping) else []
                if isinstance(raw_rows, Sequence) and raw_rows and isinstance(raw_rows[0], Sequence):
                    rows = [row for page in raw_rows for row in page] if isinstance(raw_rows[0][0], (list, tuple)) else raw_rows
                else:
                    rows = []
                hot_names = sorted({str(row.get("name") or row.get("plate_name") or "")
                                    if isinstance(row, Mapping) else str(row[1] or "")
                                    for row in rows if isinstance(row, (Mapping, list, tuple))
                                    and (isinstance(row, Mapping) or len(row) > 1)} - {""})
            except (OSError, ValueError, TypeError):
                hot_names = []
    if status != "READY":
        assignments = build_limit_reason_themes(
            {}, {}, expected_previous_date=previous_trade_date, plate_names=plate_names,
            static_themes_by_symbol=static_themes_by_symbol)
    source_counts: Counter[str] = Counter(row.get("theme_source", "NONE") for row in assignments.values())
    counts_text = ", ".join(f"{key}={source_counts[key]}" for key in sorted(source_counts)) or "无记录"
    return {"assignments": assignments, "status": status,
            "basis": ("昨日涨停池题材/历史原因" if status == "READY" else "登记表回退")
                     + f"；来源计数 {counts_text}",
            "snapshot_dir": str(run) if run else None, "hot_plate_names": hot_names,
            "source_counts": dict(source_counts)}
