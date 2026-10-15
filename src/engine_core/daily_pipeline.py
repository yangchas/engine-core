"""Daily, staged report orchestration with injectable read-only data sources."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import live_pages
from .close_review import (assemble_close_review, attach_close_sections, _attach_auction_baseline, render_close_html,
                           render_close_markdown)
from .core_cache_writer import CoreCacheWriter
from .daily_kline_capture import write_capture

STAGE_NAMES = {"auction": "auction_0926", "open": "open_0946", "close": "close_review"}


@dataclass
class DailySources:
    """All runtime inputs are injected; tests can supply small in-memory fakes."""
    td: Any = None
    redis: Any = None
    reference_root: Path | str = Path("/home/exedev/validation")
    td_error: str | None = None
    wencai_fetch: Any = None
    daily_kline_rows: Any = None


class _MissingTD:
    def states_at(self, cutoff: datetime) -> dict[str, Any]:
        return {}

    def checkpoint_ready(self, cutoff: datetime) -> bool:
        return False

    def latest_tick_ts(self) -> None:
        return None


def _connect_td() -> tuple[Any, str | None]:
    try:
        import taos
        client = taos.connect(host=os.getenv("TD_HOST", "127.0.0.1"),
                              port=int(os.getenv("TD_PORT", "6030")),
                              user=os.getenv("TD_USER", "root"),
                              password=os.getenv("TD_PASSWORD", "taosdata"),
                              database=os.getenv("TD_DATABASE", "market_data1"),
                              config=os.getenv("TD_CONFIG", "/etc/taos"))
        return client, None
    except Exception as exc:  # degrade when TD driver/service is absent
        return _MissingTD(), f"TD_READ_UNAVAILABLE:{type(exc).__name__}"


def _connect_redis() -> Any:
    import redis
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                decode_responses=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _date_from_path(path: Path) -> str | None:
    match = re.search(r"20\d{2}-?\d{2}-?\d{2}", str(path))
    if not match:
        return None
    value = match.group(0).replace("-", "")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _dated_candidates(root: Path, patterns: tuple[str, ...], trade_date: str) -> list[Path]:
    paths = {p for pattern in patterns for p in root.rglob(pattern)} if root.exists() else set()
    return sorted((p for p in paths if p.is_file() and (_date_from_path(p) or "") <= trade_date),
                  key=lambda p: (_date_from_path(p) or "", str(p)), reverse=True)


def _catalog_path(root: Path, catalog_path: str | Path | None) -> Path:
    if catalog_path:
        candidate = Path(catalog_path)
        return candidate if candidate.is_absolute() else root / candidate
    return root / "reference_catalog.json"


def _history_file_date(path: Path, trade_date: str) -> str | None:
    """Use data dates, not the enclosing study directory date, for daily history."""
    try:
        import csv
        with path.open(encoding="utf-8-sig", newline="") as handle:
            values = [str(row.get("date") or "")[:10] for row in csv.DictReader(handle)]
        eligible = [value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                    and value < trade_date]
        return max(eligible, default=None)
    except (OSError, UnicodeError, csv.Error):
        return _date_from_path(path)


def resolve_references(root: str | Path, trade_date: str, *, stage: str = "auction",
                       catalog_path: str | Path | None = None) -> tuple[dict[str, Path | None], dict[str, dict[str, Any]]]:
    """Resolve references from an explicit catalog, then deterministic dated fallback."""
    base = Path(root)
    specs = {
        "auction_baseline": ("auction_amount_by_day.csv", "*auction*baseline*.csv"),
        "daily_history": ("daily_history.csv", "daily_kline.csv", "daily_kline_*.csv", "history.csv"),
        "stock_names": ("stock_names*.json",), "stock_caps": ("stock_caps*.json", "market_caps*.json"),
    }
    catalog_file = _catalog_path(base, catalog_path)
    try:
        catalog = json.loads(catalog_file.read_text(encoding="utf-8")) if catalog_file.is_file() else {}
    except (OSError, ValueError):
        catalog = {}
    catalog_refs = catalog.get("references", catalog) if isinstance(catalog, dict) else {}
    paths: dict[str, Path | None] = {}
    metadata: dict[str, dict[str, Any]] = {}
    categories = (*specs.keys(), "daily_capture_manifest", "relation_dir", "kaipan_hot_dir",
                  "generic_names", "prev_reports_root")
    for key in categories:
        entry = catalog_refs.get(key) if isinstance(catalog_refs, dict) else None
        entry = {"path": entry} if isinstance(entry, str) else (entry or {})
        raw = entry.get("path") if isinstance(entry, dict) else None
        candidate = Path(raw) if raw else None
        if candidate and not candidate.is_absolute(): candidate = catalog_file.parent / candidate
        if candidate and candidate.exists():
            paths[key] = candidate
            metadata[key] = {"as_of": entry.get("as_of", "UNKNOWN"), "discovered_by": "catalog"}
        else:
            paths[key] = None
            metadata[key] = {"as_of": "UNKNOWN", "discovered_by": "fallback"}
    for key, patterns in specs.items():
        if paths[key] is not None: continue
        if key == "daily_history":
            candidates = {p for pattern in patterns for p in base.rglob(pattern)} if base.exists() else set()
            candidates = sorted((p for p in candidates if p.is_file()
                                 and (_history_file_date(p, trade_date) or "") < trade_date),
                                key=lambda p: (_history_file_date(p, trade_date) or "", str(p)), reverse=True)
        else:
            candidates = _dated_candidates(base, patterns, trade_date)
        if candidates:
            paths[key] = candidates[0]
            metadata[key]["as_of"] = (_history_file_date(candidates[0], trade_date) if key == "daily_history"
                                       else _date_from_path(candidates[0]) or "UNKNOWN")
    capture_manifest = paths["daily_capture_manifest"]
    if capture_manifest:
        try: capture_day = str(json.loads(capture_manifest.read_text(encoding="utf-8")).get("date") or "")
        except (OSError, ValueError): capture_day = _date_from_path(capture_manifest.parent) or ""
        if (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", capture_day) or capture_day > trade_date
                or (stage != "close" and capture_day >= trade_date)):
            paths["daily_capture_manifest"] = None
        else:
            metadata["daily_capture_manifest"]["as_of"] = capture_day
    if paths["daily_capture_manifest"] is None:
        captures = []
        for path in (base.rglob("manifest.json") if base.exists() else []):
            if not path.is_file() or not (path.parent / "daily_kline.jsonl").is_file(): continue
            try: capture_day = str(json.loads(path.read_text(encoding="utf-8")).get("date") or "")
            except (OSError, ValueError): capture_day = _date_from_path(path.parent) or ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", capture_day): continue
            if capture_day > trade_date or (stage != "close" and capture_day >= trade_date): continue
            captures.append((capture_day, path))
        if captures:
            day, paths["daily_capture_manifest"] = max(captures, key=lambda item: (item[0], str(item[1])))
            metadata["daily_capture_manifest"]["as_of"] = day
    relation_dirs = [p.parent for p in (base.rglob("registry.json") if base.exists() else [])
                     if (p.parent / "relation.csv").is_file()]
    relation_dirs = sorted((p for p in relation_dirs
                            if (_date_from_path(p) or "") <= trade_date),
                           key=lambda p: (_date_from_path(p) or "", str(p)), reverse=True)
    if paths["relation_dir"] is None and relation_dirs:
        eligible = [p for p in relation_dirs if (_date_from_path(p) or "") <= trade_date]
        if eligible: paths["relation_dir"] = sorted(eligible, key=lambda p: (_date_from_path(p) or "", str(p)), reverse=True)[0]
    if paths["kaipan_hot_dir"] is None:
        hot_dirs = sorted((p for p in base.rglob("*") if p.is_dir() and list(p.glob("hot_*.json"))
                           and (_date_from_path(p) or "") <= trade_date),
                          key=lambda p: (_date_from_path(p) or "", str(p)), reverse=True) if base.exists() else []
        if hot_dirs: paths["kaipan_hot_dir"] = hot_dirs[0]
    if paths["generic_names"] is None:
        bundled_generic = Path(__file__).resolve().parents[2] / "examples/data/plate_generic_factor_names.json"
        if bundled_generic.is_file(): paths["generic_names"] = bundled_generic
    for key in ("relation_dir", "kaipan_hot_dir"):
        if paths[key] is not None and metadata[key]["as_of"] == "UNKNOWN":
            metadata[key]["as_of"] = _date_from_path(paths[key]) or "UNKNOWN"
    return paths, metadata


def discover_references(root: str | Path, trade_date: str, *, stage: str = "auction",
                        catalog_path: str | Path | None = None) -> dict[str, Path | None]:
    return resolve_references(root, trade_date, stage=stage, catalog_path=catalog_path)[0]


def _relation_snapshot_date(relation_dir: Path | None) -> str:
    return live_pages._relation_snapshot_date(relation_dir)


def _relation_lag_trading_days(asof: str, trade_date: str, history_dates: list[str]) -> int | None:
    match = re.search(r"(20\d{2})-?(\d{2})-?(\d{2})", asof or "")
    if not match: return None
    source_day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    dates = {day[:10] for day in history_dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day[:10])}
    dates.add(trade_date)
    return sum(source_day < day <= trade_date for day in dates)


def _load_ladder_summary(root: str | Path, trade_date: str) -> list[dict[str, Any]]:
    """Load a date-matched ladder study; price-limit closes are proxies, not seal facts."""
    candidates = sorted(Path(root).glob("ladder-emotion-study-*/out_final/ladder_daily.csv"))
    for path in reversed(candidates):
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = next((item for item in csv.DictReader(stream) if item.get("date") == trade_date), None)
        except OSError:
            continue
        if not row:
            continue
        levels = []
        for level in range(1, 12):
            suffix = str(level) if level < 11 else "11plus"
            previous = row.get(f"previous_{suffix}_n")
            if not previous or previous == "0":
                continue
            levels.append({"board_height": f"{level if level < 11 else '11+'}板",
                           "previous_n": previous, "promoted_n": row.get(f"promoted_{suffix}_n"),
                           "broken_n": row.get(f"broken_{suffix}_n"),
                           "promotion_rate": row.get(f"promotion_{suffix}_rate"),
                           "status": "收盘涨停价代理；炸板路径 UNKNOWN"})
        summary = {"status": row.get("phase_eod") or "收盘", "max_price_streak": row.get("max_price_streak"),
                   "top_symbols": row.get("top_symbols"), "previous_space_height": row.get("previous_space_height"),
                   "previous_space_symbols": row.get("previous_space_symbols"),
                   "first_board_count": row.get("board_1_n"), "previous_limit_n": row.get("previous_limit_n"),
                   "promoted_n": row.get("promoted_n"), "promotion_rate": row.get("promotion_rate"),
                   "unknown_broken_n": row.get("broken_1_n"), "source": "ladder_daily.csv; close-price rule"}
        return [summary, *levels[:10]]
    return []


def _load_path_study(reference_root: str | Path, trade_date: str) -> tuple[dict[str, dict[str, int]], dict[str, str]]:
    """Load the supplied 14-day descriptive path sample, never treat it as a rule."""
    path = Path(reference_root) / "ladder-pre0945-study-20261015" / "paths_daily.csv"
    if not path.is_file():
        return {}, {}
    grouped: dict[str, list[dict[str, str]]] = {}
    today = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                kind = str(row.get("path_0945") or "UNKNOWN")
                grouped.setdefault(kind, []).append(row)
                if row.get("date") == trade_date:
                    today[str(row.get("symbol") or "").zfill(6)] = kind
    except (OSError, csv.Error):
        return {}, {}
    frequencies = {}
    for kind, rows in grouped.items():
        observed = [r for r in rows if r.get("outcome_status") not in {"MISSING", "UNKNOWN"}]
        successes = sum(r.get("outcome_status") == "CLOSED_UP_PRICE_PROXY" for r in observed)
        frequencies[kind] = {"k": successes, "n": len(observed),
                             "days": len({r.get("date") for r in rows})}
    return frequencies, today


def _attach_range_positions(stocks: list[dict[str, Any]], history_path: str | Path | None,
                            trade_date: str) -> None:
    """Attach a 20-session close-range location; missing history stays UNKNOWN."""
    if not history_path:
        return
    by_symbol: dict[str, list[tuple[str, float]]] = {}
    try:
        with Path(history_path).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = str(row.get("date") or "")[:10]
                symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
                try:
                    close = float(row.get("close"))
                except (TypeError, ValueError):
                    continue
                if day < trade_date and symbol and close > 0:
                    by_symbol.setdefault(symbol, []).append((day, close))
    except (OSError, csv.Error, UnicodeError):
        return
    for row in stocks:
        symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
        history = sorted(by_symbol.get(symbol, []))[-20:]
        pct = row.get("p0925_pct", row.get("auction_pct"))
        if len(history) != 20 or not isinstance(pct, (int, float)):
            continue
        closes = [value for _, value in history]
        low, high = min(closes), max(closes)
        if high <= low:
            continue
        auction_price = closes[-1] * (1 + float(pct) / 100)
        row["prior_range_position20_pct"] = (auction_price - low) / (high - low) * 100


def _live_section_stocks(td: Any, *, trade_date: str, stage: str, asof: str,
                         relations: Mapping[str, list[str]], plate_names: Mapping[str, str],
                         stock_names: Mapping[str, str], history_path: str | Path | None,
                         plate_metadata: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build full-universe, as-of stock rows for the live A→B and ladder sections."""
    from .live_session import daily_history_context, load_daily_history_csv, states_at

    clocks = {"0925": "09:25:06", "0932": "09:32:00", "0945": "09:45:30"}
    requested = ["0925"] if stage == "auction" else ["0925", "0932", "0945"]
    end = datetime.fromisoformat(f"{trade_date}T{asof}")
    snapshots: dict[str, dict[str, Any]] = {}
    for label in requested:
        cutoff = datetime.fromisoformat(f"{trade_date}T{clocks[label]}")
        if cutoff <= end:
            snapshots[label] = states_at(td, cutoff)
    if not snapshots:
        return []
    symbols = set().union(*(set(rows) for rows in snapshots.values()))
    context = {}
    if history_path:
        try:
            history = load_daily_history_csv(history_path)
            context = daily_history_context(history, trade_date, snapshots.get("0925", {}))["stocks"]
        except (OSError, ValueError, TypeError):
            context = {}
    from .a_to_b import select_main_plate
    plate_metadata = plate_metadata or {}
    symbol_plates: dict[str, list[str]] = {}
    for code, members in relations.items():
        name = plate_names.get(code)
        if name:
            for symbol in members:
                symbol_plates.setdefault(str(symbol).zfill(6), []).append(name)
    total_auction = sum(max(0, int(row.get("match_amount_yuan") or 0))
                        for row in snapshots.get("0925", {}).values())
    rows = []
    for symbol in sorted(str(value).zfill(6) for value in symbols):
        auction = snapshots.get("0925", {}).get(symbol, {})
        history_row = context.get(symbol, {})
        streak = history_row.get("streak")
        row: dict[str, Any] = {
            "symbol": symbol, "name": stock_names.get(symbol, auction.get("name") or symbol),
            "main_plate": select_main_plate(symbol_plates.get(symbol, []), plate_metadata),
            "main_plate_reason": None if select_main_plate(symbol_plates.get(symbol, []), plate_metadata) else "无合格主题板块",
            "plates": symbol_plates.get(symbol, []),
            "prior_streak": streak,
            "in_yest_pool": (streak >= 1 if isinstance(streak, int) else None),
            "limit_pct": 20.0 if symbol.startswith(("300", "301", "688", "689")) else
                         30.0 if symbol.startswith(("4", "8", "92")) else 10.0,
            "p0925_pct": auction.get("pct_change"),
            "auction_amount_yuan": auction.get("match_amount_yuan"),
            "auction_price_status": auction.get("status", "UNKNOWN"),
            "auction_limit_state": auction.get("auction_limit_state"),
            "rest_bid_yuan": auction.get("rest_bid_yuan"),
        }
        if total_auction and row["auction_amount_yuan"] is not None:
            row["auction_amount_bp"] = 10000 * row["auction_amount_yuan"] / total_auction
        for label, field in (("0932", "p0932_pct"), ("0945", "p0945_pct")):
            state = snapshots.get(label, {}).get(symbol)
            if state:
                row[field] = state.get("pct_change")
        rows.append(row)
    _attach_range_positions(rows, history_path, trade_date)
    return rows


def _yesterday_lb_days(report: Mapping[str, Any] | None) -> dict[str, int]:
    """Extract known streak counts from a prior report without converting gaps to zero."""
    found: dict[str, int] = {}
    if not isinstance(report, Mapping):
        return found
    candidates = []
    stocks = report.get("stocks")
    if isinstance(stocks, list):
        candidates.extend(stocks)
    chapters = report.get("chapters")
    if isinstance(chapters, Mapping):
        for key in ("yesterday_limit_pool", "yesterday_highboards"):
            if isinstance(chapters.get(key), list):
                candidates.extend(chapters[key])
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
        value = row.get("lb_days", row.get("streak", row.get("prior_streak")))
        try:
            if symbol and value is not None:
                found[symbol] = int(value)
        except (TypeError, ValueError):
            continue
    return found


def _prior_streak(code: str, yesterday_lb_days: Mapping[str, int]) -> int | None:
    """Missing yesterday streak data stays missing; current-day rows are not a fallback."""
    return yesterday_lb_days.get(str(code).zfill(6))


def _next_output(root: Path, trade_date: str, stage: str) -> Path:
    parent = root / trade_date
    candidate = parent / stage
    index = 1
    while candidate.exists():
        candidate = parent / f"{stage}-{index:03d}"
        index += 1
    return candidate


def _load_relations(folder: Path | None) -> tuple[dict[str, list[str]], dict[str, str], str]:
    if not folder:
        return {}, {}, "UNKNOWN"
    relations, names = {}, {}
    try:
        registry = json.loads((folder / "registry.json").read_text(encoding="utf-8"))
        entries = registry.get("plates", registry) if isinstance(registry, dict) else registry
        if isinstance(entries, dict):
            entries = [dict(value, code=code) for code, value in entries.items()]
        for row in entries or []:
            code = str(row.get("code", row.get("plateid", "")))
            if code:
                names[code] = str(row.get("name") or code)
        import csv
        with (folder / "relation.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = str(row.get("stockid", row.get("symbol", ""))).zfill(6)
                code = str(row.get("plateid", row.get("plate_code", "")))
                if symbol and code:
                    relations.setdefault(code, set()).add(symbol)
        relations = {code: sorted(members) for code, members in relations.items()}
        snapshot = live_pages._relation_snapshot_date(folder)
        return relations, names, snapshot
    except (OSError, ValueError, TypeError):
        return {}, {}, "UNKNOWN"


def _relation_plate_metadata(folder: Path | None, relations: Mapping[str, list[str]],
                             plate_names: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    if folder:
        try:
            registry = json.loads((folder / "registry.json").read_text(encoding="utf-8"))
            entries = registry.get("plates", registry) if isinstance(registry, dict) else registry
            if isinstance(entries, dict):
                entries = [dict(value, code=code) for code, value in entries.items()]
            for row in entries or []:
                name = str(row.get("name") or row.get("code") or row.get("plateid") or "")
                if name:
                    metadata[name] = {"class": row.get("class", row.get("kind")),
                                      "size": row.get("size", row.get("members"))}
        except (OSError, ValueError, TypeError):
            pass
    for code, members in relations.items():
        name = plate_names.get(str(code))
        if name:
            metadata.setdefault(name, {"size": len(members)})
            if metadata[name].get("size") is None:
                metadata[name]["size"] = len(members)
    return metadata


def _latest_report(root: Path, trade_date: str, stage: str) -> Path | None:
    parent = root / trade_date
    candidates = [parent / stage / "report.json"]
    candidates.extend(sorted(parent.glob(f"{stage}-*/report.json"), reverse=True))
    return next((p for p in candidates if p.is_file()), None)


def _previous_report(root: Path, trade_date: str, stages: tuple[str, ...]) -> Path | None:
    candidates: list[tuple[str, Path]] = []
    if not root.exists():
        return None
    for day_dir in root.iterdir():
        day = day_dir.name
        if not day_dir.is_dir() or day >= trade_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        for stage in stages:
            report = _latest_report(root, day, stage)
            if report:
                candidates.append((day, report))
    return max(candidates, default=("", None), key=lambda item: item[0])[1]


def _close_fallback(trade_date: str, missing: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = {"contract": "CloseReviewV1", "trade_date": trade_date,
            "status": "DEGRADED_MISSING_INPUTS", "stocks": [], "big_meat": [], "big_loss": [],
            "market": {"amount_yuan": None, "close_median": None, "daily_stock_count": None},
            "data_quality": {"included_stock_count": None, "missing": missing},
            "missing_sections": missing}
    return data, {"contract": "CloseReviewV1", "trade_date": trade_date,
                  "input_files": {}, "missing": missing}


def _td_daily_rows(td: Any, trade_date: str, injected: Any = None) -> list[dict[str, Any]]:
    """Read one day's TD daily bars; injected callback is the test/source boundary."""
    if injected is not None:
        raw_rows = injected(trade_date)
    else:
        cursor = td.cursor()
        day = date.fromisoformat(trade_date)
        database = os.getenv("TD_DATABASE", "market_data1")
        sql = ("SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg, tbname "
               f"FROM {database}.daily_kline WHERE ts >= '{day.isoformat()}' "
               f"AND ts < '{(day + timedelta(days=1)).isoformat()}'")
        cursor.execute(sql)
        raw_rows = cursor.fetchall()
    rows = []
    for raw in raw_rows or []:
        if isinstance(raw, Mapping):
            values = [raw.get(key) for key in ("ts", "open", "high", "low", "close", "volume",
                                                "amount", "turnover", "pct_chg", "tbname")]
        else:
            values = list(raw)
        if len(values) < 10 or values[0] is None or str(values[0])[:10] != trade_date:
            continue
        symbol = str(values[9] or "")
        symbol = symbol.removeprefix("d_").zfill(6)
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        rows.append({"date": trade_date, "symbol": symbol, "open": values[1], "high": values[2],
                     "low": values[3], "close": values[4], "volume": values[5],
                     "amount_yuan": values[6], "turnover_pct": values[7], "pct_chg": values[8],
                     "pre_close": None,
                     "suspended": values[5] in (None, 0) and values[6] in (None, 0)})
    return rows


def run_daily_pipeline(*, stage: str, trade_date: str, asof: str | None = None,
                       out_root: str | Path = "/home/exedev/validation/daily-reports",
                       reference_root: str | Path = "/home/exedev/validation",
                       sources: DailySources | None = None, publish: bool = False,
                       dry_run: bool = False, allow_wencai_fallback: bool = False,
                       reference_catalog: str | Path | None = None,
                       email_to: tuple[str, ...] | list[str] = (), email_send: bool = False,
                       email_attach_full_html: bool = False) -> dict[str, Any]:
    if stage not in STAGE_NAMES:
        raise ValueError("stage must be auction, open, or close")
    datetime.strptime(trade_date, "%Y-%m-%d")
    if asof is not None:
        datetime.strptime(asof, "%H:%M:%S")
    if publish and dry_run:
        raise ValueError("--publish and --dry-run cannot be combined")
    started = time.monotonic()
    refs, reference_meta = resolve_references(reference_root, trade_date, stage=stage,
                                              catalog_path=reference_catalog)
    src = sources or DailySources(reference_root=reference_root)
    if src.td is None:
        src.td, src.td_error = _connect_td()
    if src.redis is None and stage != "close":
        try:
            src.redis = _connect_redis()
        except Exception:
            src.redis = None
    q2_rows: dict[str, Any] = {}
    if stage != "close" and src.redis is not None:
        try:
            from .q2_tracker import read_q2_hashes
            q2_rows = read_q2_hashes(src.redis)
        except Exception:
            degraded_q2 = True
        else:
            degraded_q2 = False
    else:
        degraded_q2 = True
    degraded: list[str] = []
    if src.td_error:
        degraded.append(src.td_error)
    for key in ("auction_baseline", "daily_history", "relation_dir", "kaipan_hot_dir", "stock_names", "stock_caps"):
        if refs[key] is None: degraded.append(f"MISSING_REFERENCE:{key}")
    wencai_result = None
    if allow_wencai_fallback and stage in ("auction", "open") and src.td_error:
        fetch = src.wencai_fetch
        if fetch is None:
            degraded.append("WENCAI_FALLBACK_UNAVAILABLE")
        else:
            # One query per invocation; the shared fallback helper retries only within this budget.
            from .wencai_fallback import fetch_auction_fallback
            fallback = fetch_auction_fallback(fetch, date=trade_date, min_amount_yuan=5_000_000,
                                              include_st=True, max_queries=1, sleep_s=10)
            wencai_result = {"rows": fallback.rows, "labels": fallback.labels,
                             "query_log": fallback.query_log, "complete": fallback.complete,
                             "as_of_date": fallback.as_of_date}
            if not fallback.complete:
                degraded.append("WENCAI_FALLBACK_PARTIAL")
    root = Path(out_root)
    actual_stage = STAGE_NAMES[stage]
    out = _next_output(root, trade_date, actual_stage)
    out.mkdir(parents=True, exist_ok=False)
    inputs: dict[str, Any] = {}
    for key, path in refs.items():
        inputs[key] = ({"path": str(path), "sha256": _sha(path)} if path and path.is_file()
                       else {"path": str(path) if path else None, "sha256": None})
        inputs[key]["discovered_by"] = reference_meta[key]["discovered_by"]
        inputs[key]["as_of"] = reference_meta[key]["as_of"]
        if key == "daily_capture_manifest" and path and (path.parent / "daily_kline.jsonl").is_file():
            data_path = path.parent / "daily_kline.jsonl"
            inputs["daily_capture_data"] = {"path": str(data_path), "sha256": _sha(data_path)}
    relation_dir = refs["relation_dir"]
    relations, plate_names, relation_asof = _load_relations(relation_dir)
    plate_metadata = _relation_plate_metadata(relation_dir, relations, plate_names)
    path_frequencies, path_labels = _load_path_study(reference_root, trade_date)
    relation_lag = None
    if relation_asof != "UNKNOWN" and refs["daily_history"]:
        try:
            import csv
            with refs["daily_history"].open(encoding="utf-8-sig", newline="") as handle:
                session_dates = [str(row.get("date") or "")[:10] for row in csv.DictReader(handle)]
            relation_lag = _relation_lag_trading_days(relation_asof, trade_date, session_dates)
        except (OSError, ValueError):
            pass
    stock_names = {}
    if refs["stock_names"]:
        try:
            stock_names = json.loads(refs["stock_names"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            degraded.append("INVALID_REFERENCE:stock_names")
    td_source_manifest = {"available": src.td is not None and not src.td_error,
                          "data_lag_seconds": None, "coverage": None}
    daily_coverage = None
    if refs["daily_capture_manifest"]:
        try:
            capture_meta = json.loads(refs["daily_capture_manifest"].read_text(encoding="utf-8"))
            daily_coverage = capture_meta.get("row_count", capture_meta.get("rows"))
        except (OSError, ValueError):
            degraded.append("INVALID_REFERENCE:daily_capture_manifest")
    auction_ref = _latest_report(root, trade_date, STAGE_NAMES["auction"]) if stage in ("open", "close") else None
    open_ref = _latest_report(root, trade_date, STAGE_NAMES["open"]) if stage == "close" else None
    if stage in ("auction", "open"):
        render_stage = STAGE_NAMES[stage]
        asof_cutoff = datetime.fromisoformat(f"{trade_date}T{asof}") if asof else None
        hot_path = None
        hot_dir = refs["kaipan_hot_dir"]
        if hot_dir:
            hotfiles = sorted(hot_dir.glob("hot_*.json"))
            hot_path = str(hotfiles[-1]) if hotfiles else None
        baseline_csv = str(refs["auction_baseline"]) if refs["auction_baseline"] else None
        page, payload = live_pages.render_page(
            src.td or _MissingTD(), trade_date=trade_date, stage=render_stage,
            relations=relations, plate_names=plate_names, stock_names=stock_names,
            daily_history_csv=str(refs["daily_history"]) if refs["daily_history"] else None,
            auction_baseline_csv=baseline_csv, kaipan_hot_json=hot_path,
            member_snapshot_date=relation_asof, member_lag_trading_days=relation_lag,
            asof=asof_cutoff)
        payload.setdefault("chapters", {})["reference_data"] = {
            "stock_names": {"status": "READY" if refs["stock_names"] else "MISSING",
                            "reason": None if refs["stock_names"] else "名称表缺失，名称回退为代码"},
            "stock_caps": {"status": "READY" if refs["stock_caps"] else "MISSING",
                           "reason": None if refs["stock_caps"] else "市值表缺失"}}
        if refs["stock_names"] is None or refs["stock_caps"] is None:
            old_limitations = payload["limitations"]
            payload["limitations"] += " 参考数据：" + "；".join(
                name + "缺失" for name, key in (("名称表", "stock_names"), ("市值表", "stock_caps"))
                if refs[key] is None) + "。"
            import html
            page = page.replace(html.escape(old_limitations), html.escape(payload["limitations"]))
        if src.td_error:
            payload["status"] = "DEGRADED_TD_MISSING"
        if stage == "auction":
            payload["auction_terminal_0925"] = next(
                (row for row in reversed(payload.get("snapshots", []))
                 if row.get("label") == "0925"), None)
        if wencai_result is not None:
            payload["wencai_fallback"] = wencai_result
        available_snaps = [row for row in payload.get("snapshots", [])
                           if row.get("status") not in {"NOT_READY", "NOT_YET_AVAILABLE"}]
        if available_snaps:
            latest = available_snaps[-1]
            td_source_manifest.update({"data_lag_seconds": latest.get("data_lag_seconds"),
                                       "coverage": {"symbols": latest.get("symbol_count"),
                                                    "priced_symbols": latest.get("priced_symbol_count")}})
        if stage == "open":
            handoff = None
            if auction_ref:
                try:
                    auction_data = json.loads(auction_ref.read_text(encoding="utf-8"))
                    handoff = {"path": str(auction_ref), "stage": auction_data.get("stage"),
                               "final_0925": auction_data.get("auction_terminal_0925")}
                except (OSError, ValueError):
                    degraded.append("INVALID_HANDOFF:auction_report")
            payload["handoff"] = {"auction_0925": handoff}
            if handoff is None:
                degraded.append("MISSING_SAME_DAY_AUCTION_REPORT")
        from .a_to_b import build_a_to_b
        from .ladder_section import build_ladder_section
        point = f"{trade_date}T{asof or '09:45:00'}"
        live_stocks = payload.get("stocks") if isinstance(payload.get("stocks"), list) else []
        if not live_stocks:
            live_stocks = _live_section_stocks(
                src.td or _MissingTD(), trade_date=trade_date, stage=stage,
                asof=asof or ("09:26:00" if stage == "auction" else "09:46:00"),
                relations=relations, plate_names=plate_names, stock_names=stock_names,
                history_path=refs["daily_history"], plate_metadata=plate_metadata)
        _attach_range_positions(live_stocks, refs["daily_history"], trade_date)
        symbol_plates = {}
        for plate_code, members in relations.items():
            plate_name = plate_names.get(plate_code)
            if plate_name:
                for symbol in members:
                    symbol_plates.setdefault(symbol, []).append(plate_name)
        payload["a_to_b"] = (build_a_to_b(live_stocks, trade_date=trade_date, stage=stage,
                          asof=point, relation_asof=relation_asof, relations=symbol_plates,
                          plate_metadata=plate_metadata)
                             if live_stocks else [{"a_symbol": "—", "reason": "缺关系数据：本时点报告无全量个股与板块成员"}])
        highboards = payload.get("chapters", {}).get("yesterday_highboards", []) if isinstance(payload.get("chapters"), Mapping) else []
        for row in highboards if isinstance(highboards, list) else []:
            if isinstance(row, dict):
                row["path_type"] = path_labels.get(str(row.get("symbol", "")).zfill(6), "UNKNOWN")
        payload["ladder_section"] = build_ladder_section(
            highboards if isinstance(highboards, list) else [], stage=stage, asof=point,
            frequencies=path_frequencies, market_stocks=live_stocks)
        from .email_render import render_email_html
        email_page, _ = render_email_html(payload, stage, trade_date, {
            "asof": asof, "sources": {"td": td_source_manifest}, "degraded": degraded})
        (out / "report.html").write_text(email_page, encoding="utf-8")
        report_json = live_pages._json_report_payload(payload)
        report_text = json.dumps(report_json, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    else:
        capture_manifest = refs["daily_capture_manifest"]
        daily_capture_dir = capture_manifest.parent if capture_manifest else None
        td_daily_error = None
        if not src.td_error:
            try:
                td_rows = _td_daily_rows(src.td, trade_date, src.daily_kline_rows)
                if td_rows:
                    daily_capture_dir = out / "daily_capture"
                    capture_meta = write_capture(daily_capture_dir, td_rows, {
                        "date": trade_date, "source": "td_daily_kline", "symbol_count": len(td_rows),
                        "valid": sum(row.get("close") is not None for row in td_rows),
                        "missing": 0, "partial": False})
                    refs["daily_capture_manifest"] = daily_capture_dir / "manifest.json"
                    inputs["daily_capture_manifest"] = {
                        "path": str(refs["daily_capture_manifest"]),
                        "sha256": _sha(refs["daily_capture_manifest"])}
                    capture_data_path = daily_capture_dir / "daily_kline.jsonl"
                    inputs["daily_capture_data"] = {"path": str(capture_data_path),
                                                     "sha256": _sha(capture_data_path)}
                    daily_coverage = capture_meta.get("valid")
                else:
                    td_daily_error = "TD_DAILY_KLINE_EMPTY"
            except Exception as exc:
                td_daily_error = f"TD_DAILY_KLINE_UNAVAILABLE:{type(exc).__name__}"
        else:
            td_daily_error = src.td_error
        if daily_capture_dir:
            try:
                local_meta = json.loads((daily_capture_dir / "manifest.json").read_text(encoding="utf-8"))
                if str(local_meta.get("date")) != trade_date:
                    daily_capture_dir = None
            except (OSError, ValueError):
                daily_capture_dir = None
        if daily_capture_dir is None:
            if td_daily_error:
                degraded.append(td_daily_error)
            degraded.append("MISSING_DAILY_KLINE: need TD daily_kline or a local same-date capture")
        elif td_daily_error:
            degraded.append(f"DAILY_KLINE_FALLBACK_LOCAL:{td_daily_error}")
        required = {"daily_capture": daily_capture_dir, "report_dir": None,
                    "checkpoints_csv": None, "auction_snapshot_csv": None}
        report_dir_candidates = _dated_candidates(Path(reference_root), ("stocks.json",), trade_date)
        checkpoint_candidates = _dated_candidates(Path(reference_root), ("checkpoints.csv",), trade_date)
        auction_candidates = _dated_candidates(Path(reference_root), ("*auction*snapshot*.csv",), trade_date)
        report_dir = report_dir_candidates[0].parent if report_dir_candidates else None
        live_path = open_ref or auction_ref
        reports_root = refs.get("prev_reports_root") or root
        prev_path = _previous_report(Path(reports_root), trade_date, (STAGE_NAMES["close"], STAGE_NAMES["open"]))
        missing = [name for name, value in required.items() if value is None]
        if report_dir is None: missing.append("report_dir")
        if not checkpoint_candidates: missing.append("checkpoints_csv")
        if not auction_candidates: missing.append("auction_snapshot_csv")
        if missing:
            data, close_manifest = _close_fallback(trade_date, missing)
            data["reference_data"] = {
                "stock_names": {"status": "READY" if refs["stock_names"] else "MISSING"},
                "stock_caps": {"status": "READY" if refs["stock_caps"] else "MISSING"}}
            degraded.extend(f"MISSING_CLOSE_INPUT:{name}" for name in missing)
            reference_state = "；".join(
                f"{name}{'可用' if refs[key] else '缺失'}"
                for key, name in (("stock_names", "名称表"), ("stock_caps", "市值表")))
            markdown = ("# 收盘复盘\n\n" + "\n".join(f"- {name}: 缺失" for name in missing)
                        + f"\n- 参考数据: {reference_state}。\n")
            page = "<!doctype html><meta charset='utf-8'><title>收盘复盘</title><pre>" + markdown + "</pre>"
        else:
            data, close_manifest = assemble_close_review(
                trade_date, str(report_dir), str(checkpoint_candidates[0]),
                str(auction_candidates[0]), str(daily_capture_dir),
                daily_history_csv=str(refs["daily_history"]) if refs["daily_history"] else None,
                relation_dir=str(relation_dir) if relation_dir else None,
                stock_caps_json=str(refs["stock_caps"]) if refs["stock_caps"] else None,
                live_report_json=str(live_path) if live_path else None,
                prev_live_json=str(prev_path) if prev_path else None,
                kaipan_hot_dir=str(refs["kaipan_hot_dir"]) if refs["kaipan_hot_dir"] else None)
            data["reference_data"] = {
                "stock_names": {"status": "READY" if refs["stock_names"] else "MISSING"},
                "stock_caps": {"status": "READY" if refs["stock_caps"] else "MISSING"}}
            markdown = render_close_markdown(data)
            page = render_close_html(data, markdown)
        ladder = _load_ladder_summary(reference_root, trade_date)
        previous_streaks = {}
        if prev_path and prev_path.is_file():
            try:
                previous_streaks = _yesterday_lb_days(_json(prev_path))
            except (OSError, ValueError, json.JSONDecodeError):
                previous_streaks = {}
        data["ladder_summary"] = ladder
        if not ladder:
            limitations = data.get("limitations")
            if not isinstance(limitations, list):
                limitations = [limitations] if limitations else []
            limitations.append("未找到该交易日的连板梯队日表；晋级/断板与最高板状态不可用")
            data["limitations"] = limitations
        elif data.get("stocks"):
            stock_by_code = {str(row.get("symbol")): row for row in data["stocks"]}
            head = ladder[0]
            highboard_rows = []
            for code in str(head.get("top_symbols") or "").split("|"):
                stock = stock_by_code.get(code)
                if not stock:
                    continue
                prior_streak = _prior_streak(code, previous_streaks)
                highboard_rows.append({"symbol": code, "name": stock.get("name"),
                    "main_plate": stock.get("main_plate"), "prior_streak": prior_streak,
                    "p0925_pct": stock.get("p0925_pct"), "p0945_pct": stock.get("p0945_pct"),
                    "close_pct": stock.get("close_pct"), "in_yest_pool": stock.get("in_yest_pool"),
                    "status": "今日最高板；收盘涨停价代理，封单/炸板路径 UNKNOWN"})
            data["ladder_summary"].extend(highboard_rows)
        if isinstance(data.get("stocks"), list):
            _attach_range_positions(data["stocks"], refs["daily_history"], trade_date)
        ladder_rows = []
        symbols = []
        for summary_row in ladder:
            if summary_row.get("top_symbols"):
                symbols.extend(str(summary_row["top_symbols"]).split("|"))
        stock_by_code = {str(row.get("symbol")): row for row in data.get("stocks", [])}
        for code in dict.fromkeys(symbols):
            if code in stock_by_code:
                row = dict(stock_by_code[code])
                row["prior_streak"] = _prior_streak(code, previous_streaks)
                row["path_type"] = path_labels.get(str(code).zfill(6), "UNKNOWN")
                ladder_rows.append(row)
        close_asof = f"{trade_date}T15:00:00"
        symbol_plates = {}
        for plate_code, members in relations.items():
            plate_name = plate_names.get(plate_code)
            if plate_name:
                for symbol in members:
                    symbol_plates.setdefault(symbol, []).append(plate_name)
        attach_close_sections(data, relation_asof=relation_asof, relations=symbol_plates,
            plate_metadata=plate_metadata, ladder_rows=ladder_rows,
            ladder_summary=ladder, path_frequencies=path_frequencies)
        _attach_auction_baseline(data, refs.get("auction_baseline"))
        quality = data.setdefault("data_quality", {})
        quality["relation_snapshot_date"] = reference_meta.get("relation_dir", {}).get("as_of")
        quality["relation_lag_trading_days"] = relation_lag
        quality["checkpoint_coverage"] = close_manifest.get("checkpoint_coverage") if isinstance(close_manifest, Mapping) else None
        data.setdefault("handoff", {})
        data["handoff"].update({"same_day_auction": str(auction_ref) if auction_ref else None,
                                "same_day_open": str(open_ref) if open_ref else None,
                                "previous_day_live": str(prev_path) if prev_path else None})
        (out / "report.md").write_text(markdown, encoding="utf-8")
        from .email_render import render_email_html
        email_page, _ = render_email_html(data, "close", trade_date, {"degraded": degraded})
        (out / "report.html").write_text(email_page, encoding="utf-8")
        report_text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        close_manifest["input_files"] = close_manifest.get("input_files", {})
    if len(report_text.encode("utf-8")) > 300_000:
        raise ValueError("report.json exceeds 300 KB")
    (out / "report.json").write_text(report_text, encoding="utf-8")
    if stage != "close":
        close_manifest = {}
    manifest = {"contract": "DailyPipelineManifestV1", "trade_date": trade_date,
                "stage": stage, "asof": asof, "outputs": str(out), "input_files": inputs,
                "sources": {"td": td_source_manifest,
                            "q2_redis": {"available": src.redis is not None and not degraded_q2,
                                         "coverage": len(q2_rows) if src.redis is not None and not degraded_q2 else None},
                            "kaipan_relation": {"path": str(relation_dir) if relation_dir else None,
                                                "asof": relation_asof,
                                                "lag_trading_days": relation_lag if relation_lag is not None else "UNKNOWN",
                                                "coverage": len(relations)},
                            "daily_kline": {"available": refs["daily_capture_manifest"] is not None,
                                            "path": str(refs["daily_capture_manifest"]) if refs["daily_capture_manifest"] else None,
                                            "coverage": daily_coverage}},
                "degraded": sorted(set(degraded)), "close_review_manifest": close_manifest,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "report_json_bytes": len(report_text.encode("utf-8")), "allow_wencai_fallback": allow_wencai_fallback}
    if degraded_q2 and stage != "close":
        manifest["degraded"].append("Q2_REDIS_UNAVAILABLE_OR_READ_FAILED")
    if publish:
        if src.redis is None:
            raise RuntimeError("--publish requires an isolated Redis client")
        key = f"core:cache:report:{trade_date}:{stage}"
        CoreCacheWriter(src.redis, now_ms=int(time.time() * 1000)).client.set(key, report_text, ex=172800)
        manifest["published_key"] = key
    manifest["email"] = {"status": "not_requested"}
    if email_to:
        try:
            from .report_mailer import build_report_message, load_smtp_config, send_message
            mail_config = load_smtp_config() if email_send else None
            sender = mail_config.from_addr if mail_config else "reports@localhost"
            mail = build_report_message("Core", trade_date, stage, out / "report.html",
                                       out / "report.json", email_to, sender, manifest=manifest,
                                       attach_full_html=email_attach_full_html)
            receipt = send_message(mail_config, mail, dry_run=not email_send,
                                  allow_recipients=email_to)
            manifest["email"] = {"status": receipt.status}
        except Exception as exc:
            manifest["email"] = {"status": "failed", "error_class": type(exc).__name__}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False,
                                                    separators=(",", ":"), allow_nan=False) + "\n",
                                        encoding="utf-8")
    return {"out_dir": out, "manifest": manifest, "report": json.loads(report_text)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=tuple(STAGE_NAMES), required=True)
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--asof")
    parser.add_argument("--out-root", default="/home/exedev/validation/daily-reports")
    parser.add_argument("--reference-catalog")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-wencai-fallback", action="store_true")
    parser.add_argument("--email-to", action="append", default=[], metavar="ADDR")
    parser.add_argument("--email-send", action="store_true", help="send email; default is dry-run")
    parser.add_argument("--email-attach-full-html", action="store_true", help="attach full report.html when at most 1.5 MB")
    return parser


def main(argv: list[str] | None = None, *, sources: DailySources | None = None) -> int:
    args = build_parser().parse_args(argv)
    reference_root = os.getenv("CORE_REFERENCE_ROOT", "/home/exedev/validation")
    result = run_daily_pipeline(stage=args.stage, trade_date=args.trade_date, asof=args.asof,
                                out_root=args.out_root, reference_root=reference_root,
                                sources=sources, publish=args.publish, dry_run=args.dry_run,
                                allow_wencai_fallback=args.allow_wencai_fallback,
                                reference_catalog=args.reference_catalog,
                                email_to=args.email_to, email_send=args.email_send,
                                email_attach_full_html=args.email_attach_full_html)
    print(f"trade_date={args.trade_date} stage={args.stage} out={result['out_dir']} "
          f"degraded={len(result['manifest']['degraded'])} report_json_bytes={result['manifest']['report_json_bytes']}")
    return 0
