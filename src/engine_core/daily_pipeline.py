"""Daily, staged report orchestration with injectable read-only data sources."""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

from . import live_pages
from .close_review import (assemble_close_review, attach_close_sections, _attach_auction_baseline,
                           derive_close_review, render_close_html, render_close_markdown)
from .core_cache_writer import CoreCacheWriter
from .daily_kline_capture import write_capture

STAGE_NAMES = {"auction": "auction_0926", "open": "open_0946", "close": "close_review"}
TZ = ZoneInfo("Asia/Shanghai")


def _degraded_checkpoint_report(report: Mapping[str, Any], trade_date: str,
                               checkpoint: str, asof: str | None) -> dict[str, Any]:
    """Keep control metadata while hiding observations and every derived value."""
    return {"trade_date": trade_date,
            "asof": f"{trade_date}T{asof}" if asof else report.get("asof"),
            "checkpoint": checkpoint, "status": "DEGRADED"}


def _auction_assembly_mode() -> str:
    """Select the auction product assembly; V6 is the default with legacy rollback."""
    mode = os.getenv("CORE_AUCTION_ASSEMBLY", "v6").strip().lower()
    if mode not in {"v6", "legacy"}:
        raise ValueError("CORE_AUCTION_ASSEMBLY must be 'v6' or 'legacy'")
    return mode


def _should_refresh_live_auction_facts(facts_result: Any, *, is_current_day: bool,
                                       force: bool) -> bool:
    if force:
        return True
    if not is_current_day:
        return False
    return (facts_result is None or not facts_result.rows or
            facts_result.meta.get("source") != "live_td_read")


def _fact_auction_amount(fact: Mapping[str, Any]) -> Any:
    matched = fact.get("auction_match_amount_yuan")
    return matched if matched is not None else fact.get("p0925_cumulative_amount_yuan")


def _numeric(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _auction_baseline_history(rows: Any, *, trade_date: str, asof: str,
                              replay_event_time: bool = False) -> list[float]:
    """Return one daily td_0925 auction match total under PIT or event-time rules."""
    totals: dict[str, float] = {}
    try:
        cutoff = datetime.fromisoformat(str(asof))
    except (TypeError, ValueError):
        return []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, Mapping):
            continue
        day = str(row.get("date") or "")[:10]
        if replay_event_time:
            pit_visible = True
        else:
            try:
                available = datetime.fromisoformat(str(row.get("available_at") or ""))
                pit_visible = available <= cutoff
            except (TypeError, ValueError):
                pit_visible = False
        if (not day or day >= trade_date or row.get("row_kind") != "market_observation"
                or row.get("source") != "td_0925" or not pit_visible):
            continue
        value = _numeric(row.get("auction_amount_yuan"))
        if value is None:
            sum_yi = _numeric(row.get("sum_yi"))
            value = sum_yi * 1e8 if sum_yi is not None else None
        if value is None or value <= 0:
            continue
        # Duplicate revisions are one observation per market date, never additive.
        totals[day] = max(totals.get(day, 0.0), value)
    return [totals[day] for day in sorted(totals)]


def _d06_persisted_assignments(slice_result: Any, *, trade_date: str, used_date: str,
                               asof: str, input_version: str,
                               visibility_sha256: str) -> dict[str, dict[str, Any]]:
    """D06 prep products are audit-only until producer and consumer share inputs."""
    return {}


@dataclass
class DailySources:
    """All runtime inputs are injected; tests can supply small in-memory fakes."""
    td: Any = None
    redis: Any = None
    reference_root: Path | str | None = None
    slice_reader: Any = None
    td_error: str | None = None
    wencai_fetch: Any = None
    daily_kline_rows: Any = None
    td_connection_factory: Any = None
    # Explicit clock seam for deterministic historical simulation.
    clock_now: datetime | None = None
    force_live_auction_facts: bool = False


class _MissingTD:
    def states_at(self, cutoff: datetime) -> dict[str, Any]:
        return {}

    def checkpoint_ready(self, cutoff: datetime) -> bool:
        return False

    def latest_tick_ts(self) -> None:
        return None


def _shrink_live_report(report: dict[str, Any], *, plate_cap: int = 20, row_cap: int = 30) -> dict[str, Any]:
    """Bound list sizes in the persisted live-stage JSON; the HTML page is rendered beforehand."""
    omitted = report.setdefault("omitted_blocks", [])
    def locate(target: Any) -> str:
        stack = [("", report)]
        while stack:
            path, value = stack.pop()
            if value is target:
                return path
            if isinstance(value, dict):
                stack.extend((f"{path}.{key}" if path else str(key), child)
                             for key, child in value.items())
            elif isinstance(value, list):
                stack.extend((f"{path}[{index}]", child) for index, child in enumerate(value))
        return "UNKNOWN"
    def cut(container: Any, key: str, cap: int) -> None:
        if isinstance(container, dict) and isinstance(container.get(key), list) and len(container[key]) > cap:
            removed = container[key][cap:]
            encoded = json.dumps(removed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            omitted.append({"path": f"{locate(container)}.{key}".lstrip("."), "rows": len(removed),
                           "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()})
            container[f"{key}_total"] = len(container[key])
            container[key] = container[key][:cap]
    themes = report.get("limit_reason_themes")
    if isinstance(themes, (dict, list)) and len(json.dumps(themes, ensure_ascii=False)) > 20_000:
        rows = list(themes.values()) if isinstance(themes, dict) else themes
        by_source: dict[str, int] = {}
        for row in rows:
            key = str(row.get("theme_source")) if isinstance(row, dict) else "UNKNOWN"
            by_source[key] = by_source.get(key, 0) + 1
        report["limit_reason_themes"] = {"count": len(rows), "by_source": by_source,
                                         "note": "full mapping omitted from report.json; see ladder clusters"}
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        omitted.append({"path": "limit_reason_themes", "rows": len(rows),
                        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()})
    chapters = report.get("chapters") if isinstance(report.get("chapters"), dict) else {}
    cut(chapters.get("market_price_volume"), "top_plates_by_share", plate_cap)
    cut(chapters.get("plate_quadrants"), "rows", 30)
    cut(chapters.get("watchlist"), "behavior_candidates", 5)
    cut(chapters.get("watchlist"), "amount_top50", 15)
    cut(chapters.get("plate_linkage"), "rows", 10)
    cut(chapters.get("plate_linkage"), "opening_changes", 8)
    cut(chapters.get("plate_linkage"), "cross_table_rows", 0)
    cut(chapters.get("plate_cross_table"), "rows", 10)
    cross_table = chapters.get("plate_cross_table")
    if isinstance(cross_table, dict) and isinstance(cross_table.get("main_rows"), list):
        cut(cross_table, "main_rows", 10)
    dictionary = report.get("plate_dictionary")
    if isinstance(dictionary, dict) and isinstance(dictionary.get("rows"), list):
        original = dictionary["rows"]
        prior_hash = dictionary.get("omitted_rows_sha256")
        prior_count = dictionary.get("rows_total")
        if original or not (isinstance(prior_hash, str) and prior_count is not None):
            encoded = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            prior_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            prior_count = len(original)
        omitted.append({"path": "plate_dictionary.rows", "rows": prior_count,
                        "sha256": prior_hash})
        dictionary["rows_total"] = prior_count
        dictionary["omitted_rows_sha256"] = prior_hash
        dictionary["rows"] = []
    extremes = chapters.get("extremes_board")
    if isinstance(extremes, dict):
        stage = str(report.get("stage", ""))
        inactive = (("open", "close") if stage.startswith("auction") else
                    ("close",) if stage.startswith("open") else ())
        for name in inactive:
            block = extremes.get(name)
            if isinstance(block, dict):
                for key in ("rows", "rebound_top8", "weak_top8", "amount_cohort"):
                    if isinstance(block.get(key), list):
                        block[key] = []
        active = extremes.get("open" if stage.startswith("open") else "auction")
        if isinstance(active, dict):
            cut(active, "rows", 15)
    for snap in report.get("snapshots", []) if isinstance(report.get("snapshots"), list) else []:
        cut(snap, "top_plates_by_share", plate_cap)
    cut(report, "snapshots", 10)
    front_page = report.get("front_page")
    if isinstance(front_page, dict):
        cut(front_page.get("plate_strength"), "rows", 20)
    a_to_b = report.get("a_to_b")
    if isinstance(a_to_b, list) and len(a_to_b) > 5:
        report["a_to_b_total"] = len(a_to_b)
        report["a_to_b"] = a_to_b[:5]
    # Full D09 rows are stored in the immutable cold product. The hot report
    # carries only checkpoint matrices and coverage metadata.
    feedback = report.get("streak_feedback")
    if isinstance(feedback, dict):
        for point in feedback.get("checkpoints", {}).values():
            if isinstance(point, dict) and isinstance(point.get("rows"), list):
                point["rows_count"] = len(point["rows"])
                point.pop("rows", None)
    terminal = report.get("auction_terminal_0925")
    if isinstance(terminal, dict):
        cut(terminal, "top_plates_by_share", plate_cap)
    return report


def _connect_td() -> tuple[Any, str | None]:
    try:
        import taos
        config = os.getenv("TD_CONFIG")
        if not config:
            return _MissingTD(), "TD_CONFIG_REQUIRED"
        options: dict[str, Any] = {"database": os.getenv("TD_DATABASE", "market_data1"),
                                   "config": config}
        for env_name, option in (("TD_HOST", "host"), ("TD_PORT", "port"),
                                 ("TD_USER", "user"), ("TD_PASSWORD", "password")):
            if os.getenv(env_name):
                options[option] = int(os.environ[env_name]) if option == "port" else os.environ[env_name]
        client = taos.connect(**options)
        return client, None
    except Exception as exc:  # degrade when TD driver/service is absent
        return _MissingTD(), f"TD_READ_UNAVAILABLE:{type(exc).__name__}"


def _read_live_auction_fact_rows(td: Any, trade_date: str, asof: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read only the 0925 snapshot and latest required tick fields through asof."""
    day = trade_date.replace("-", "")
    cutoff = datetime.fromisoformat(asof).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    start = f"{trade_date} 09:25:00.000"
    snapshot_sql = (
        "SELECT ts,symbol,px_milli,chg_bp,match_amt_yuan,rest_bid_amt_yuan,"
        "rest_ask_amt_yuan,limit_state,trade_date,auction_tag "
        f'FROM market_data1.auction_snapshot_v2 WHERE trade_date="{day}" '
        f'AND auction_tag="0925" AND ts<="{cutoff}"'
    )
    tick_sql = (
        "SELECT last_row(ts) AS ts,last_row(px_milli) AS px_milli,"
        "last_row(amt_yuan) AS amt_yuan,last_row(pc_milli) AS pc_milli,"
        "last_row(bp1_milli) AS bp1_milli,"
        "last_row(bv1) AS bv1,symbol "
        f'FROM market_data1.stock_tick_v2 WHERE ts>="{start}" AND ts<="{cutoff}" '
        "GROUP BY symbol"
    )

    def fetch(sql: str) -> list[dict[str, Any]]:
        cursor = td.cursor()
        try:
            cursor.execute(sql)
            columns = [str(item[0]).lower().split(".")[-1] for item in (cursor.description or ())]
            rows = []
            for raw in cursor.fetchall():
                row = dict(raw) if isinstance(raw, Mapping) else dict(zip(columns, raw))
                rows.append({str(key).lower().split(".")[-1]: value for key, value in row.items()})
            return rows
        finally:
            close = getattr(cursor, "close", None)
            if callable(close): close()

    snapshots = fetch(snapshot_sql)
    ticks = fetch(tick_sql)
    normalized_snapshots = []
    for row in snapshots:
        normalized_snapshots.append({**row, "symbol": str(row.get("symbol") or "").zfill(6),
            "auction_amount_yuan": row.get("match_amt_yuan"), "price_milli": row.get("px_milli"),
            "change_bp": row.get("chg_bp"), "source_ts": row.get("ts")})
    normalized_ticks = []
    for row in ticks:
        normalized_ticks.append({**row, "symbol": str(row.get("symbol") or "").zfill(6),
            "source_ts": row.get("ts"), "tick_volume_unit": "lots"})
    tick_by_symbol = {row["symbol"]: row for row in normalized_ticks}
    for row in normalized_snapshots:
        tick = tick_by_symbol.get(row["symbol"])
        if tick is not None:
            row["pre_close_milli"] = tick.get("pc_milli")
    return normalized_snapshots, normalized_ticks


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
    # Recursive file discovery is deliberately disabled. File based replay must
    # name an explicit catalog entry and opt into CORE_ALLOW_FILE_FALLBACK.
    return []


def _catalog_path(root: Path, catalog_path: str | Path | None) -> Path:
    if catalog_path:
        candidate = Path(catalog_path)
        return candidate if candidate.is_absolute() else root / candidate
    return root / "reference_catalog.json"


def _history_file_date(path: Path, trade_date: str) -> str | None:
    """Use data dates, not the enclosing study directory date, for daily history."""
    try:
        from .live_session import load_daily_history_csv
        values = [str(row.get("date") or "")[:10] for row in
                  load_daily_history_csv(path, trade_date=trade_date)]
        eligible = [value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                    and value < trade_date]
        return max(eligible, default=None)
    except (OSError, UnicodeError, ValueError):
        return _date_from_path(path)


def resolve_references(root: str | Path, trade_date: str, *, stage: str = "auction",
                       catalog_path: str | Path | None = None,
                       redis: Any = None) -> tuple[dict[str, Path | None], dict[str, dict[str, Any]]]:
    """Resolve explicit replay references only; never discover files recursively."""
    base = Path(root)
    specs = {
        "stock_day_facts": ("stock_day_facts",),
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
        as_of = str(entry.get("as_of", "UNKNOWN")) if isinstance(entry, dict) else "UNKNOWN"
        as_of_day = as_of[:10]
        point_in_time_ok = (as_of == "UNKNOWN" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of_day)
                            or as_of_day < trade_date or (stage == "close" and as_of_day == trade_date))
        if candidate and candidate.exists() and point_in_time_ok:
            paths[key] = candidate
            metadata[key] = {"as_of": as_of, "discovered_by": "catalog"}
        else:
            paths[key] = None
            metadata[key] = {"as_of": "UNKNOWN", "discovered_by": "fallback"}
    # Core Redis keys only point at immutable catalog manifests; report inputs still
    # resolve to the artifact so large history is never copied through Redis.
    if redis is not None:
        for key in categories:
            if not callable(getattr(redis, "get", None)):
                break
            pointer_key = f"core:cache:data:reference:{key}:{trade_date}"
            try:
                pointer_raw = redis.get(pointer_key)
                if pointer_raw is None:
                    latest_raw = redis.get(f"core:cache:data:reference:{key}:latest")
                    pointer_raw = latest_raw
                pointer = json.loads(pointer_raw) if pointer_raw else {}
                reference_uri = pointer.get("path") if isinstance(pointer, dict) else None
                candidate = Path(reference_uri) if reference_uri else None
                if candidate and candidate.exists():
                    as_of = str(pointer.get("as_of", "UNKNOWN"))
                    as_of_day = as_of[:10]
                    point_in_time_ok = (as_of == "UNKNOWN" or as_of_day < trade_date
                                        or (stage == "close" and as_of_day == trade_date))
                    if point_in_time_ok:
                        paths[key] = candidate
                        metadata[key] = {"as_of": as_of, "discovered_by": "redis",
                                         "version": pointer.get("version")}
            except Exception:
                continue
    # Explicit catalogs are replay-only compatibility. File fallback is opt-in and
    # callers must separately mark it in report provenance.
    if paths["generic_names"] is None:
        bundled_generic = Path(__file__).resolve().parents[2] / "examples/data/plate_generic_factor_names.json"
        if bundled_generic.is_file(): paths["generic_names"] = bundled_generic
    return paths, metadata


def discover_references(root: str | Path, trade_date: str, *, stage: str = "auction",
                        catalog_path: str | Path | None = None,
                        redis: Any = None) -> dict[str, Path | None]:
    return resolve_references(root, trade_date, stage=stage, catalog_path=catalog_path,
                              redis=redis)[0]


def _relation_snapshot_date(relation_dir: Path | None) -> str:
    return live_pages._relation_snapshot_date(relation_dir)


def _relation_lag_trading_days(asof: str, trade_date: str, history_dates: list[str]) -> int | None:
    match = re.search(r"(20\d{2})-?(\d{2})-?(\d{2})", asof or "")
    if not match: return None
    source_day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    dates = {day[:10] for day in history_dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day[:10])}
    dates.add(trade_date)
    return sum(source_day < day <= trade_date for day in dates)


def _load_ladder_summary(root: str | Path | list[dict[str, Any]] | None, trade_date: str) -> list[dict[str, Any]]:
    """Load a date-matched ladder study; price-limit closes are proxies, not seal facts."""
    candidates = [row for row in root if row.get("date") == trade_date] if isinstance(root, list) else []
    for row in candidates:
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


def _load_path_study(reference_root: str | Path | list[dict[str, Any]] | None, trade_date: str) -> tuple[dict[str, dict[str, int]], dict[str, str]]:
    """Load the supplied 14-day descriptive path sample, never treat it as a rule."""
    if not isinstance(reference_root, list):
        return {}, {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    today = {}
    for row in reference_root:
        kind = str(row.get("path_0945") or "UNKNOWN")
        grouped.setdefault(kind, []).append(row)
        if row.get("date") == trade_date:
            today[str(row.get("symbol") or "").zfill(6)] = kind
    frequencies = {}
    for kind, rows in grouped.items():
        observed = [r for r in rows if r.get("outcome_status") not in {"MISSING", "UNKNOWN"}]
        successes = sum(r.get("outcome_status") == "CLOSED_UP_PRICE_PROXY" for r in observed)
        frequencies[kind] = {"k": successes, "n": len(observed),
                             "days": len({r.get("date") for r in rows})}
    return frequencies, today


def _attach_range_positions(stocks: list[dict[str, Any]], history_path: str | Path | None,
                            trade_date: str, *, report_features: list[Mapping[str, Any]] | None = None) -> None:
    """Attach a 20-session close-range location; missing history stays UNKNOWN."""
    if report_features and report_features[0].get("feature_type") == "daily_report_features_v1":
        feature_map = {str(item.get("symbol", "")).zfill(6): item for item in report_features
                       if item.get("symbol") != "__MARKET__"}
        for row in stocks:
            symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
            feature = feature_map.get(symbol, {})
            pct = row.get("p0925_pct", row.get("auction_pct"))
            low, high, prior_close = (feature.get("range20_low"), feature.get("range20_high"),
                                      feature.get("range20_last_close"))
            if (feature.get("position_close_days") == 20 and all(isinstance(value, (int, float))
                    for value in (low, high, prior_close, pct)) and high > low):
                auction_price = prior_close * (1 + float(pct) / 100)
                row["prior_range_position20_pct"] = (auction_price - low) / (high - low) * 100
        return
    if not history_path:
        return
    try:
        from .live_session import load_daily_history_csv
        by_symbol: dict[str, list[tuple[str, float]]] = {}
        for row in load_daily_history_csv(history_path, trade_date=trade_date):
            day = str(row.get("date") or "")[:10]
            symbol = str(row.get("symbol") or "").zfill(6)
            close = row.get("close")
            if day < trade_date and symbol and isinstance(close, (int, float)) and close > 0:
                by_symbol.setdefault(symbol, []).append((day, close))
    except (OSError, csv.Error, UnicodeError, ValueError):
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


def _observed_seal_paths(td: Any, trade_date: str, asof: str,
                         symbols: set[str]) -> dict[str, list[str]]:
    """Read actual opening ticks for the yesterday pool and retain sealed timestamps."""
    symbols = {"".join(ch for ch in str(symbol) if ch.isdigit())[-6:].zfill(6)
               for symbol in symbols}
    symbols.discard("000000")
    if not symbols or td is None:
        return {}
    from .limit_price import is_limit_up
    cursor = td.cursor() if callable(getattr(td, "cursor", None)) else td
    clauses = ",".join("'" + symbol + "'" for symbol in sorted(symbols))
    end = str(asof)[-8:]
    cursor.execute(
        "select ts,px_milli,pc_milli,bp1_milli,bv2,symbol from stock_tick_v2 "
        f"where ts>='{trade_date} 09:30:00' and ts<='{trade_date} {end}' "
        f"and symbol in ({clauses}) order by ts,symbol")
    rows = cursor.fetchall()
    columns = ("ts", "px_milli", "pc_milli", "bp1_milli", "bv2", "symbol")
    sealed: dict[str, list[str]] = {}
    for raw in rows:
        row = raw if isinstance(raw, Mapping) else dict(zip(columns, raw))
        symbol = str(row.get("symbol") or "")[-6:].zfill(6)
        try:
            price = float(row["px_milli"]) / 1000
            previous = float(row["pc_milli"]) / 1000
            bid = float(row["bv2"]) * float(row["bp1_milli"]) / 10
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if symbol in symbols and bid > 0 and is_limit_up(price, previous, symbol):
            stamp = row.get("ts")
            text = stamp.isoformat(sep=" ") if hasattr(stamp, "isoformat") else str(stamp)
            sealed.setdefault(symbol, []).append(text)
    return sealed


def _live_section_stocks(td: Any, *, trade_date: str, stage: str, asof: str,
                         relations: Mapping[str, list[str]], plate_names: Mapping[str, str],
                         stock_names: Mapping[str, str], history_path: str | Path | None,
                         plate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
                         report_features: list[Mapping[str, Any]] | None = None,
                         path_symbols: set[str] | None = None,
                         checkpoint: str | None = None,
                         td_connection_factory=None) -> list[dict[str, Any]]:
    """Build full-universe, as-of stock rows for the live A→B and ladder sections."""
    from .live_session import (OPEN_CHECKPOINTS, daily_history_context, load_daily_history_csv,
                               open_checkpoint_for_asof, states_at)

    clocks = {"0924": "09:24:10", **dict(OPEN_CHECKPOINTS)}
    selected_checkpoint = checkpoint or (open_checkpoint_for_asof(asof) if stage == "open" else "0925")
    requested = (["0924", "0925"] if stage == "auction" else
                 ["0924", *(label for label, _ in OPEN_CHECKPOINTS)])
    end = datetime.fromisoformat(f"{trade_date}T{asof}")
    snapshots: dict[str, dict[str, Any]] = {}
    fetch = []
    for label in requested:
        cutoff = datetime.fromisoformat(f"{trade_date}T{clocks[label]}")
        if cutoff <= end:
            fetch.append((label, cutoff))
    def fetch_one(item):
        label, cutoff = item
        connection = td_connection_factory() if td_connection_factory else td
        try:
            return label, states_at(connection, cutoff)
        finally:
            if td_connection_factory:
                close = getattr(connection, "close", None)
                if callable(close): close()
    if len(fetch) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(fetch))) as pool:
            snapshots.update(pool.map(fetch_one, fetch))
    elif fetch:
        snapshots.update(fetch_one(fetch[0]) for _ in (0,))
    if not snapshots:
        return []
    seal_paths: dict[str, list[str]] = {}
    if stage == "open" and path_symbols:
        path_td = td_connection_factory() if td_connection_factory else td
        try:
            seal_paths = _observed_seal_paths(path_td, trade_date, asof, path_symbols)
        except Exception:
            seal_paths = {}
        finally:
            if td_connection_factory:
                close = getattr(path_td, "close", None)
                if callable(close): close()
    symbols = set().union(*(set(rows) for rows in snapshots.values()))
    context = {}
    if report_features:
        context = daily_history_context(report_features, trade_date, snapshots.get("0925", {}))["stocks"]
    elif history_path:
        try:
            history = load_daily_history_csv(history_path, trade_date=trade_date)
            context = daily_history_context(history, trade_date, snapshots.get("0925", {}))["stocks"]
        except (OSError, ValueError, TypeError):
            context = {}
    from .a_to_b import select_main_plate
    from .auction_tracking import _at_limit_pair

    def checkpoint_limit_state(symbol: str, state: Mapping[str, Any]) -> int | None:
        up, down = _at_limit_pair({**state, "symbol": symbol})
        if up is True:
            return 1
        if down is True:
            return -1
        if up is False and down is False:
            return 0
        return None

    plate_metadata = plate_metadata or {}
    symbol_plates: dict[str, list[str]] = {}
    for code, members in relations.items():
        name = plate_names.get(code)
        if name:
            for symbol in members:
                symbol_plates.setdefault(str(symbol).zfill(6), []).append(name)
    auction_amounts = [row.get("match_amount_yuan") for row in snapshots.get("0925", {}).values()]
    total_auction = (sum(max(0, int(value)) for value in auction_amounts)
                     if auction_amounts and all(value is not None for value in auction_amounts) else None)
    rows = []
    for symbol in sorted(str(value).zfill(6) for value in symbols):
        auction = snapshots.get("0925", {}).get(symbol, {})
        cutoff_state = (snapshots.get(selected_checkpoint, {}).get(symbol, {})
                        if stage == "open" else auction)
        history_row = context.get(symbol, {})
        streak = history_row.get("streak")
        main_plate = select_main_plate(symbol_plates.get(symbol, []), plate_metadata)
        row: dict[str, Any] = {
            "symbol": symbol, "name": stock_names.get(symbol, auction.get("name") or symbol),
            "main_plate": main_plate,
            "main_plate_reason": None if main_plate else "无合格主题板块",
            "plates": symbol_plates.get(symbol, []),
            "prior_streak": streak,
            "in_yest_pool": (streak >= 1 if isinstance(streak, int) else None),
            "limit_pct": 20.0 if symbol.startswith(("300", "301", "688", "689")) else
                         30.0 if symbol.startswith(("4", "8", "92")) else 10.0,
            "p0925_pct": auction.get("pct_change"),
            "auction_amount_yuan": auction.get("match_amount_yuan"),
            "p0924_amount_yuan": snapshots.get("0924", {}).get(symbol, {}).get("match_amount_yuan"),
            "p0924_pct": snapshots.get("0924", {}).get(symbol, {}).get("pct_change"),
            "p0925_amount_yuan": auction.get("match_amount_yuan"),
            "p0945_amount_yuan": snapshots.get("0945", {}).get(symbol, {}).get("amt_yuan"),
            "auction_price_status": auction.get("status", "UNKNOWN"),
            "auction_limit_state": checkpoint_limit_state(symbol, auction),
            "open_limit_state": checkpoint_limit_state(symbol, cutoff_state),
            "open_checkpoint": selected_checkpoint if stage == "open" else None,
            "current_checkpoint": selected_checkpoint if stage == "open" else "0925",
            "current_pct": cutoff_state.get("pct_change") if cutoff_state else None,
            "current_amount_yuan": (cutoff_state.get("amt_yuan", cutoff_state.get("match_amount_yuan"))
                                     if cutoff_state else None),
            "rest_bid_yuan": auction.get("rest_bid_yuan"),
            "p0925_price": auction.get("price_milli") / 1000 if isinstance(auction.get("price_milli"), (int, float)) else None,
            "p0925_pre_close": auction.get("prev_close_milli") / 1000 if isinstance(auction.get("prev_close_milli"), (int, float)) else None,
            "p0925_rest_bid": auction.get("rest_bid_yuan"), "p0925_rest_ask": auction.get("rest_ask_yuan"),
            "p0925_limit_state": checkpoint_limit_state(symbol, auction),
            "p0925_high": auction.get("high_milli") / 1000 if isinstance(auction.get("high_milli"), (int, float)) else None,
            "p0925_source_ts": auction.get("source_ts"),
            "observed_seal_timestamps": seal_paths.get(symbol, []),
            "price": (cutoff_state.get("price_milli") / 1000
                      if isinstance(cutoff_state.get("price_milli"), (int, float)) else None),
            "pre_close": (cutoff_state.get("prev_close_milli") / 1000
                          if isinstance(cutoff_state.get("prev_close_milli"), (int, float)) else None),
        }
        for label, field in (("0932", "p0932_pct"), ("0935", "p0935_pct"),
                             ("0938", "p0938_pct"), ("0940", "p0940_pct"),
                             ("0945", "p0945_pct")):
            row[field] = None
        if total_auction and row["auction_amount_yuan"] is not None:
            row["auction_amount_bp"] = 10000 * row["auction_amount_yuan"] / total_auction
        for label, field in (("0932", "p0932_pct"), ("0935", "p0935_pct"),
                             ("0938", "p0938_pct"), ("0940", "p0940_pct"),
                             ("0945", "p0945_pct")):
            state = snapshots.get(label, {}).get(symbol)
            if state:
                row[field] = state.get("pct_change")
                row[f"p{label}_price"] = (state.get("price_milli") / 1000
                                           if isinstance(state.get("price_milli"), (int, float)) else None)
                row[f"p{label}_pre_close"] = (state.get("prev_close_milli") / 1000
                                               if isinstance(state.get("prev_close_milli"), (int, float)) else None)
                row[f"p{label}_rest_bid"] = state.get("rest_bid_yuan")
                row[f"p{label}_rest_ask"] = state.get("rest_ask_yuan")
                row[f"p{label}_amount"] = state.get("amt_yuan", state.get("match_amount_yuan"))
                row[f"p{label}_high"] = state.get("high_milli") / 1000 if isinstance(state.get("high_milli"), (int, float)) else None
                row[f"p{label}_low"] = state.get("low_milli") / 1000 if isinstance(state.get("low_milli"), (int, float)) else None
                row[f"p{label}_source_ts"] = state.get("source_ts")
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
        match = re.search(r"(20\d{2})-?(\d{2})-?(\d{2})", snapshot or "")
        if match:
            snapshot = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
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


def _eligible_front_plate(name: str, metadata: Mapping[str, Mapping[str, Any]]) -> bool:
    row = metadata.get(name, {})
    try:
        size = int(row.get("size"))
    except (TypeError, ValueError):
        return False
    return row.get("class", row.get("kind")) in {"THEME", "NICHE"} and 8 <= size <= 150


def _latest_report(root: Path, trade_date: str, stage: str) -> Path | None:
    parent = root / trade_date
    candidates = [parent / stage / "report.json", *parent.glob(f"{stage}-*/report.json")]
    if stage == STAGE_NAMES["open"]:
        candidates.extend(parent.glob("open_*/report.json"))
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime_ns) if existing else None


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


def _read_optional_json(path: Path, degraded: list[str], label: str) -> Any:
    """Read a referenced JSON report, recording controlled missing/invalid states."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        degraded.append(f"MISSING_REFERENCE_JSON:{label}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        degraded.append(f"INVALID_REFERENCE_JSON:{label}")
    return None


def _close_fallback(trade_date: str, missing: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = {"contract": "CloseReviewV1", "trade_date": trade_date,
            "status": "DEGRADED_MISSING_INPUTS", "stocks": [], "big_meat": [], "big_loss": [],
            "market": {"amount_yuan": None, "close_median": None, "daily_stock_count": None},
            "data_quality": {"included_stock_count": None, "missing": missing},
            "missing_sections": missing}
    return data, {"contract": "CloseReviewV1", "trade_date": trade_date,
                  "input_files": {}, "missing": missing}


def load_close_catalog_inputs(catalog: Any, trade_date: str,
                              prior_trade_date: str | None, *, asof: str | None = None,
                              basis: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the date-pinned Core close artifacts and normalize their available fields."""
    basis = basis or ("POSTHOC_REVIEW" if os.getenv("POSTHOC_DEV") == "1" else "LIVE_PIT")
    if basis not in {"LIVE_PIT", "POSTHOC_REVIEW"}:
        raise ValueError("basis must be LIVE_PIT or POSTHOC_REVIEW")
    if basis == "POSTHOC_REVIEW" and trade_date >= date.today().isoformat():
        raise ValueError("POSTHOC_REVIEW requires a trade_date strictly before the current market day")
    cutoff = datetime.fromisoformat(asof) if asof else datetime.fromisoformat(f"{trade_date}T15:00:00+08:00")
    specifications = {
        "daily_kline": trade_date,
        "stock_day_facts": trade_date,
        "auction_stock_facts": trade_date,
        "auction_panorama": trade_date,
        "limit_up_pool": prior_trade_date,
        "plate_members": trade_date,
        "stock_theme_assignment": trade_date,
    }
    selected: dict[str, dict[str, Any]] = {}
    provenance: dict[str, Any] = {}
    for dataset, source_date in specifications.items():
        manifest_path = catalog.locate(dataset, source_date) if source_date else None
        if not manifest_path or not manifest_path.is_file():
            provenance[dataset] = {"status": "MISSING", "trade_date": source_date}
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("trade_date") != source_date:
                raise ValueError("catalog date mismatch")
            available = manifest.get("available_at")
            try:
                available_dt = datetime.fromisoformat(str(available).replace("Z", "+00:00"))
                normalized_cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=TZ)
                visible = available_dt <= normalized_cutoff
            except (TypeError, ValueError):
                visible = False
            if basis == "LIVE_PIT" and not visible:
                provenance[dataset] = {"status": "UNAVAILABLE", "trade_date": source_date,
                                       "reason": "LATE_OR_UNKNOWN_AVAILABLE_AT", "available_at": available,
                                       "manifest": str(manifest_path)}
                continue
            rows = catalog.read_rows(manifest_path)
        except (OSError, ValueError, TypeError, KeyError):
            provenance[dataset] = {"status": "INVALID", "trade_date": source_date,
                                   "manifest": str(manifest_path)}
            continue
        selected[dataset] = {"manifest": manifest, "path": str(manifest_path), "rows": rows}
        provenance[dataset] = {"status": "READY" if rows else "EMPTY", "trade_date": source_date,
                               "version": manifest.get("version"), "sha256": manifest.get("sha256"),
                               "manifest": str(manifest_path), "row_count": len(rows),
                               "available_at": manifest.get("available_at"),
                               "storage_format": manifest.get("storage_format"),
                               "quality": manifest.get("quality")}

    daily_rows = selected.get("daily_kline", {}).get("rows", [])
    facts = {str(row.get("symbol") or "").zfill(6): row
             for row in selected.get("stock_day_facts", {}).get("rows", []) if row.get("symbol")}
    auction = {str(row.get("symbol") or "").zfill(6): row
               for row in selected.get("auction_stock_facts", {}).get("rows", []) if row.get("symbol")}
    pool = {str(row.get("symbol") or row.get("code") or "").zfill(6): row
            for row in selected.get("limit_up_pool", {}).get("rows", []) if row.get("symbol") or row.get("code")}
    assignments = {str(row.get("symbol") or "").zfill(6): row
                   for row in selected.get("stock_theme_assignment", {}).get("rows", []) if row.get("symbol")}
    member_labels: dict[str, set[str]] = {}
    for row in selected.get("plate_members", {}).get("rows", []):
        symbol = str(row.get("symbol") or "").zfill(6)
        label = str(row.get("plate_member_label") or "")
        member_labels.setdefault(symbol, set()).update(part.strip() for part in label.split("、") if part.strip())

    normalized: list[dict[str, Any]] = []
    for bar in daily_rows:
        symbol = str(bar.get("symbol") or bar.get("code") or "").zfill(6)
        if len(symbol) != 6 or not symbol.isdigit() or bar.get("date", trade_date) != trade_date:
            continue
        fact = facts.get(symbol, {})
        auc = auction.get(symbol, {})
        previous = pool.get(symbol, {})
        assignment = assignments.get(symbol, {})
        primary = assignment.get("primary_theme")
        primary = primary if isinstance(primary, Mapping) else {}
        secondary = assignment.get("secondary_themes")
        secondary = secondary if isinstance(secondary, list) else []
        themes = [str(item.get("name")) for item in secondary if isinstance(item, Mapping) and item.get("name")]
        if primary.get("name"):
            themes.insert(0, str(primary["name"]))
        themes.extend(sorted(member_labels.get(symbol, set())))
        def first_value(*values: Any) -> Any:
            return next((value for value in values if value is not None), None)
        close = first_value(bar.get("close"), fact.get("close"))
        pre_close = first_value(bar.get("pre_close"), fact.get("pre_close"))
        pct = first_value(bar.get("pct_chg"), fact.get("pct_chg"))
        high = bar.get("high")
        low = bar.get("low")
        normalized.append({
            "symbol": symbol, "name": auc.get("name") or "名称未知", "open": bar.get("open"),
            "high": high, "low": low, "close": close, "pre_close": pre_close,
            "close_pct": pct,
            "high_pct": ((high / pre_close - 1) * 100 if isinstance(high, (int, float))
                         and isinstance(pre_close, (int, float)) and pre_close else None),
            "low_pct": ((low / pre_close - 1) * 100 if isinstance(low, (int, float))
                        and isinstance(pre_close, (int, float)) and pre_close else None),
            "amount_yuan": first_value(bar.get("amount_yuan"), fact.get("amount_yuan")),
            "volume_shares": first_value(bar.get("volume"), fact.get("volume_shares")),
            "suspended": bar.get("suspended"),
            "auction_pct": auc.get("pct_change"), "p0925_pct": auc.get("pct_change"),
            "auction_price": auc.get("price"), "auction_amount_yuan": auc.get("auction_amount_yuan"),
            "auction_price_status": "VALID" if auc.get("valid_price") else "UNKNOWN",
            "auction_limit_state": auc.get("seal"), "limit_pct": fact.get("limit_pct"),
            "in_yest_pool": (bool(previous) if provenance.get("limit_up_pool", {}).get("status") == "READY" else None),
            "lb_days": previous.get("lb_days"),
            "yesterday_limit_up": (bool(previous) if provenance.get("limit_up_pool", {}).get("status") == "READY" else None), "yesterday_lb_days": previous.get("lb_days"),
            "main_plate": primary.get("name"), "plates": list(dict.fromkeys(themes)),
            "daily_source": "CORE_CATALOG:daily_kline", "checkpoint_status": "MISSING",
        })

    fields = {
        "close": ("daily_kline", "close"), "pre_close": ("daily_kline", "pre_close"),
        "pct_chg": ("daily_kline", "pct_chg"), "amount_yuan": ("daily_kline", "amount_yuan"),
        "volume_shares": ("daily_kline", "volume"), "suspended": ("daily_kline", "suspended"),
        "auction_pct": ("auction_stock_facts", "pct_change"),
        "auction_price": ("auction_stock_facts", "price"),
        "auction_amount_yuan": ("auction_stock_facts", "auction_amount_yuan"),
        "p0932_pct": ("stock_day_facts", "p0932_pct"),
        "p0935_pct": ("stock_day_facts", "p0935_pct"),
        "p0940_pct": ("stock_day_facts", "p0940_pct"),
        "p0945_pct": ("stock_day_facts", "p0945_pct"),
        "yesterday_limit_up": ("limit_up_pool", "symbol"),
        "theme": ("stock_theme_assignment", "primary_theme"),
        "plate_members": ("plate_members", "symbol"),
    }
    field_coverage = {}
    expected = len(daily_rows)
    for name, (dataset, column) in fields.items():
        source_rows = selected.get(dataset, {}).get("rows", [])
        if name.startswith("p09"):
            by_symbol = {str(row.get("symbol") or "").zfill(6): row for row in source_rows}
            valid = sum(isinstance(by_symbol.get(row["symbol"], {}).get(column), (int, float))
                        for row in normalized)
        elif dataset == "limit_up_pool":
            valid = sum(row["in_yest_pool"] is not None for row in normalized)
        elif dataset == "plate_members":
            valid = sum(bool(member_labels.get(row["symbol"])) for row in normalized)
        elif dataset == "stock_theme_assignment":
            valid = sum(bool(assignments.get(row["symbol"], {}).get(column)) for row in normalized)
        else:
            by_symbol = {str(row.get("symbol") or "").zfill(6): row for row in source_rows}
            valid = sum(by_symbol.get(row["symbol"], {}).get(column) is not None for row in normalized)
        entry = {"expected_n": expected, "valid_n": valid,
                                "missing_n": max(0, expected - valid),
                                "source_manifest": provenance.get(dataset, {}).get("manifest")}
        if name == "yesterday_limit_up":
            entry.update(positive_n=sum(r["in_yest_pool"] is True for r in normalized),
                         negative_n=sum(r["in_yest_pool"] is False for r in normalized),
                         unknown_n=sum(r["in_yest_pool"] is None for r in normalized))
        field_coverage[name] = entry
    panorama_rows = selected.get("auction_panorama", {}).get("rows", [])
    panorama = panorama_rows[0] if panorama_rows else None
    if panorama and isinstance(panorama.get("product_json"), str):
        try:
            panorama = json.loads(panorama["product_json"])
        except ValueError:
            panorama = None
    manifest = {"source": "CORE_CATALOG", "trade_date": trade_date, "basis": basis,
                "asof": cutoff.isoformat(),
                "late_sources": [name for name, info in provenance.items()
                                 if basis == "POSTHOC_REVIEW" and info.get("available_at") and
                                 (datetime.fromisoformat(str(info["available_at"]).replace("Z", "+00:00")) > (cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=TZ)))],
                "prior_trade_date": prior_trade_date, "datasets": provenance,
                "field_coverage": field_coverage, "checkpoint_status": "MISSING",
                "auction_panorama": panorama}
    return normalized, manifest


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


def _dry_run_artifact_root(out_root: str | Path, dry_run: bool) -> Path | None:
    """A dry-run replay must not append versions to the production catalog (None = default CORE_DATA_ROOT)."""
    return Path(out_root) / ".dry_run_artifacts" if dry_run else None


def run_daily_pipeline(*, stage: str, trade_date: str, asof: str | None = None,
                       out_root: str | Path = "/home/exedev/validation/daily-reports",
                       reference_root: str | Path | None = None,
                       sources: DailySources | None = None, publish: bool = False,
                       dry_run: bool = False, allow_wencai_fallback: bool = False,
                       reference_catalog: str | Path | None = None,
                       email_to: tuple[str, ...] | list[str] = (), email_send: bool = False,
                       close_basis: str = "LIVE_PIT") -> dict[str, Any]:
    if stage not in STAGE_NAMES:
        raise ValueError("stage must be auction, open, or close")
    datetime.strptime(trade_date, "%Y-%m-%d")
    if asof is not None:
        datetime.strptime(asof, "%H:%M:%S")
    if publish and dry_run:
        raise ValueError("--publish and --dry-run cannot be combined")
    if stage == "close" and close_basis == "POSTHOC_REVIEW" and trade_date >= date.today().isoformat():
        raise ValueError("POSTHOC_REVIEW requires a trade_date strictly before the current market day")
    started = time.monotonic()
    pipeline_timings: dict[str, float] = {}
    auction_panorama_artifact = None
    src = sources or DailySources(reference_root=reference_root)
    if src.redis is None and stage != "close":
        try:
            src.redis = _connect_redis()
        except Exception:
            src.redis = None
    if os.getenv("CORE_ALLOW_FILE_FALLBACK") == "1" and (reference_root is not None or reference_catalog):
        explicit_root = reference_root or Path(reference_catalog).expanduser().parent
        refs, reference_meta = resolve_references(explicit_root, trade_date, stage=stage,
                                                  catalog_path=reference_catalog, redis=src.redis)
    else:
        refs, reference_meta = resolve_references("/__core_slice_no_file_fallback__", trade_date,
                                                  stage=stage, redis=None)
    from .slices import SliceProviders, SliceReader
    slice_reader = src.slice_reader or SliceReader(
        SliceProviders(redis=None if stage == "close" else src.redis).providers(), snapshot_id=f"{trade_date}:{stage}")
    point = asof or ("09:26:00" if stage == "auction" else "09:46:00" if stage == "open" else "17:40:00")
    slice_asof = f"{trade_date}T{point}+08:00"
    slice_names = ("stock_name_map", "plate_registry", "plate_members", "stock_theme_assignment",
                   "daily_report_features", "daily_history",
                   "auction_baseline", "plate_amount_baseline", "yest_limit_pool", "limit_reason_history", "hot_plates_today",
                   "hot_plates_yesterday", "hot_rank", "trading_calendar", "path_stats")
    if stage == "auction":
        slice_names += ("auction_stock_facts",)
    slice_results = {}
    slice_timings = {}
    ordered_slices = ("trading_calendar",) + tuple(name for name in slice_names if name != "trading_calendar")
    prior_candidates = []
    for name in ordered_slices:
        slice_started = time.monotonic()
        params = {}
        fallback_key = {"daily_history": "daily_history", "auction_baseline": "auction_baseline",
                        "stock_name_map": "stock_names"}.get(name)
        if fallback_key and refs.get(fallback_key) is not None:
            params["fallback_file"] = str(refs[fallback_key])
        if name in {"daily_history", "daily_report_features", "hot_plates_today", "plate_members",
                    "hot_plates_yesterday", "yest_limit_pool", "stock_theme_assignment"}:
            calendar_rows = slice_results.get("trading_calendar")
            if calendar_rows:
                prior_candidates = sorted({str(row.get("date") or "")[:10]
                                           for row in calendar_rows.rows
                                           if str(row.get("date") or "")[:10] < trade_date})
            params["prior_trade_date"] = prior_candidates[-1] if prior_candidates else None
            if name == "daily_history":
                # Keep a short raw window for report stages when the PIT feature
                # snapshot is ready; otherwise retain the legacy recomputation span.
                feature_status = slice_results.get("daily_report_features")
                features_ready = bool(feature_status and feature_status.meta.get("status") == "READY")
                params["history_days"] = (5 if features_ready and stage in {"auction", "open"} else 75)
            if name == "daily_report_features":
                params["prior_trade_date"] = prior_candidates[-1] if prior_candidates else None
            if name in {"hot_plates_yesterday", "yest_limit_pool", "plate_members"}:
                params["source_date"] = params["prior_trade_date"]
        if name == "stock_theme_assignment":
            params["source_date"] = trade_date
        slice_results[name] = slice_reader.get(name, trade_date=trade_date, asof=slice_asof, **params)
        if name == "trading_calendar" and stage == "close":
            prior_candidates = sorted({str(row.get("date") or "")[:10]
                                       for row in slice_results[name].rows
                                       if str(row.get("date") or "")[:10] < trade_date})
        slice_timings[name] = round(time.monotonic() - slice_started, 3)
    # Persist the exact slice facts used by both the email preview and the
    # report manifest. Catalog paths retain import provenance that SliceResult
    # intentionally abstracts away from its provider contract.
    try:
        from .data_pipeline.storage import ArtifactCatalog
        catalog_root = Path(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data")).expanduser()
        catalog_datasets = ArtifactCatalog(catalog_root).read().get("datasets", {})
    except (OSError, ValueError, TypeError):
        catalog_datasets = {}
    dataset_aliases = {"daily_history": "daily_history_compact", "daily_report_features": "daily_report_features",
                       "auction_baseline": "auction_baseline", "plate_members": "plate_members",
                       "hot_rank": "hot_rank", "hot_plates_today": "hot_plates",
                       "hot_plates_yesterday": "hot_plates"}
    slice_manifest: dict[str, dict[str, Any]] = {}
    used_dates: dict[str, list[str]] = {}
    for name, result in slice_results.items():
        meta = result.meta
        dates = sorted({str(row.get("trade_date") or row.get("date") or "")[:10]
                        for row in result.rows if row.get("trade_date") or row.get("date")})
        lineage_dates = sorted({str(item.get("used_date"))[:10] for item in meta.get("lineage", [])
                                if isinstance(item, Mapping) and item.get("used_date")})
        actual_dates = lineage_dates or dates
        if actual_dates:
            used_dates[name] = actual_dates
        provenance = meta.get("source")
        version_source = None
        version = meta.get("version")
        alias = dataset_aliases.get(name, name)
        entries = catalog_datasets.get(alias, {}).get("dates", {}) if isinstance(catalog_datasets, Mapping) else {}
        matched = next((entry for versions in entries.values() for entry in versions
                        if entry.get("version") == version), None) if isinstance(entries, Mapping) else None
        if matched and matched.get("manifest"):
            manifest_path = Path(matched["manifest"])
            version_source = manifest_path.parent.parent.name.removeprefix("source=")
        quality = meta.get("quality") if isinstance(meta.get("quality"), Mapping) else {}
        coverage = quality.get("coverage") if isinstance(quality.get("coverage"), Mapping) else None
        if name == "auction_baseline" and coverage is None:
            # Row count is an artifact size, not a sample-day count.
            coverage = None
        slice_manifest[name] = {"source": provenance, "version_source": version_source,
                                "status": meta.get("status"),
                                "asof": meta.get("asof"), "available_at": meta.get("available_at"),
                                "rows": len(result.rows), "reason": meta.get("reason"),
                                "version": version, "quality": quality,
                                "used_date": actual_dates or None, "coverage": coverage}
    if src.td is None and not (stage == "close" and refs.get("stock_day_facts")):
        src.td, src.td_error = _connect_td()
        if not src.td_error:
            src.td_connection_factory = lambda: _connect_td()[0]
    if stage == "auction" and _auction_assembly_mode() == "v6":
        facts_result = slice_results.get("auction_stock_facts")
        facts_rows = facts_result.rows if facts_result is not None else []
        now_dt = src.clock_now or datetime.now(TZ)
        is_current_day = trade_date == now_dt.date().isoformat()
        refresh_live_facts = _should_refresh_live_auction_facts(
            facts_result, is_current_day=is_current_day, force=src.force_live_auction_facts)
        if refresh_live_facts:
            facts_rows = []
        if refresh_live_facts:
            requested_cutoff = datetime.fromisoformat(slice_asof)
            hard_cutoff = datetime.fromisoformat(f"{trade_date}T09:26:00+08:00")
            cutoff = min(requested_cutoff, hard_cutoff, now_dt)
            if cutoff.date().isoformat() != trade_date:
                cutoff = datetime.fromisoformat(f"{trade_date}T09:25:00+08:00")
            cutoff_text = cutoff.isoformat()
            try:
                if src.td_error or src.td is None:
                    raise RuntimeError("TD_UNAVAILABLE")
                snapshots, ticks = _read_live_auction_fact_rows(src.td, trade_date, cutoff_text)
                if len(snapshots) < 5000:
                    raise ValueError("SNAPSHOT_COVERAGE_LOW")
                if len(ticks) < int(len(snapshots) * 0.90):
                    raise ValueError("TICK_COVERAGE_LOW")
                from .auction_stock_facts import build_auction_stock_facts, store_auction_stock_facts
                name_result = slice_results.get("stock_name_map")
                facts = build_auction_stock_facts(
                    trade_date, cutoff_text, snapshots, ticks, tick_volume_unit="lots",
                    st_symbols={str(row.get("symbol") or "").zfill(6) for row in
                        (name_result.rows if name_result is not None else [])
                        if row.get("is_st") or row.get("stock_type") == "ST"},
                    unlimited_symbols={str(row.get("symbol") or "").zfill(6) for row in
                        (name_result.rows if name_result is not None else [])
                        if row.get("is_new_stock")},
                    now=now_dt.isoformat(), read_at=now_dt.isoformat())
                stored = store_auction_stock_facts(facts, redis=src.redis, artifact_root=_dry_run_artifact_root(out_root, dry_run))
                from .slices import SliceResult
                quality = {**facts["summary"], "coverage": len(facts["rows"]), "partial": True}
                facts_meta = {"slice_id": "auction_stock_facts", "status": "PARTIAL",
                    "source": "live_td_read", "version": stored["version"], "asof": cutoff_text,
                    "as_of": cutoff_text, "available_at": stored.get("available_at"),
                    "pit_date": trade_date, "quality": quality, "reason": None,
                    "lineage": [{"source": "live_td_read", "used_date": trade_date,
                                 "available_at": stored.get("available_at")}]}
                slice_results["auction_stock_facts"] = SliceResult.create(facts["rows"], facts_meta)
                slice_manifest["auction_stock_facts"] = {"source": "live_td_read",
                    "version_source": "auction_stock_facts", "status": "PARTIAL", "asof": cutoff_text,
                    "available_at": stored.get("available_at"), "rows": len(facts["rows"]),
                    "reason": None, "version": stored["version"], "quality": quality,
                    "used_date": [trade_date], "coverage": len(facts["rows"])}
            except Exception as exc:
                error_code = str(exc) if str(exc) in {"SNAPSHOT_COVERAGE_LOW", "TICK_COVERAGE_LOW", "TD_UNAVAILABLE"} else "TD_READ_FAILED"
                slice_manifest["auction_stock_facts"] = {"source": "live_td_read", "status": "MISSING",
                    "reason": f"AUCTION_STOCK_FACTS_{error_code}", "rows": 0, "version": None}
                from .slices import SliceResult
                slice_results["auction_stock_facts"] = SliceResult.create([], {
                    "slice_id": "auction_stock_facts", "status": "MISSING",
                    "source": "live_td_read", "reason": f"AUCTION_STOCK_FACTS_{error_code}",
                    "quality": {"coverage": 0, "partial": True}})
    q2_coverage: int | None = None
    if stage != "close" and src.redis is not None:
        try:
            from .q2_tracker import count_q2_hashes
            q2_coverage = count_q2_hashes(src.redis)
        except Exception:
            degraded_q2 = True
        else:
            degraded_q2 = False
    else:
        degraded_q2 = True
    pipeline_timings["td_fetch"] = round(time.monotonic() - started, 3)
    compute_started = time.monotonic()
    degraded: list[str] = []
    if src.td_error:
        degraded.append(src.td_error)
    for key in ("auction_baseline", "daily_history", "relation_dir", "kaipan_hot_dir", "stock_names", "stock_caps"):
        slice_name = {"relation_dir": "plate_members", "kaipan_hot_dir": "hot_plates_today",
                      "stock_names": "stock_name_map", "stock_caps": "stock_name_map"}.get(key, key)
        available = slice_results.get(slice_name)
        if refs[key] is None and (available is None or available.meta.get("status") == "MISSING"):
            degraded.append(f"MISSING_SLICE:{slice_name}")
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
    output_stage = actual_stage
    chosen_checkpoint = None
    if stage == "open":
        from .live_session import open_checkpoint_for_asof
        chosen_checkpoint = open_checkpoint_for_asof(asof)
        if asof is not None:
            output_stage = f"open_{chosen_checkpoint}"
    elif stage == "auction" and asof is not None:
        output_stage = f"auction_{(asof or '09:26:00').replace(':', '')[:4]}"
    out = _next_output(root, trade_date, output_stage)
    out.mkdir(parents=True, exist_ok=False)
    inputs: dict[str, Any] = {}
    for key, path in refs.items():
        inputs[key] = ({"path": str(path), "sha256": _sha(path)} if path and path.is_file()
                       else {"path": str(path) if path else None, "sha256": None})
        inputs[key]["discovered_by"] = reference_meta[key]["discovered_by"]
        inputs[key]["as_of"] = reference_meta[key]["as_of"]
        if reference_meta[key].get("version"):
            inputs[key]["version"] = reference_meta[key]["version"]
        if key == "daily_capture_manifest" and path and (path.parent / "daily_kline.jsonl").is_file():
            data_path = path.parent / "daily_kline.jsonl"
            inputs["daily_capture_data"] = {"path": str(data_path), "sha256": _sha(data_path)}
    relation_dir = refs["relation_dir"]
    relations, plate_names, relation_asof = _load_relations(relation_dir)
    members_rows = slice_results["plate_members"].rows
    registry_rows = slice_results["plate_registry"].rows
    if members_rows:
        relations = {}
        for row in members_rows:
            symbol = str(row.get("symbol") or row.get("stock_code") or row.get("stockid") or "").zfill(6)
            code = str(row.get("plate_code") or row.get("plateid") or row.get("code") or "")
            if symbol.strip("0") and code:
                relations.setdefault(code, []).append(symbol)
        plate_names = {str(row.get("code") or row.get("plate_code") or row.get("plateid")): str(row.get("name") or "")
                       for row in registry_rows if row.get("name")}
        plate_names.setdefault("801827", "AI视频")
        relation_asof = str(slice_results["plate_members"].meta.get("pit_date") or
                             slice_results["plate_members"].meta.get("asof") or "UNKNOWN")
    plate_metadata = _relation_plate_metadata(relation_dir, relations, plate_names)
    for row in registry_rows:
        name = str(row.get("name") or "")
        if name:
            plate_metadata[name] = {"class": row.get("class", row.get("kind")),
                                    "size": row.get("size", row.get("members")),
                                    "code": row.get("code", row.get("plate_code"))}
    path_frequencies, path_labels = _load_path_study(slice_results["path_stats"].rows, trade_date)
    relation_lag = None
    if relation_asof != "UNKNOWN" and slice_results["daily_history"].rows:
        try:
            import csv
            from .live_session import load_daily_history_csv
            session_dates = sorted({str(row.get("date") or "")[:10] for row in
                                    slice_results["trading_calendar"].rows
                                    if str(row.get("date") or "")[:10] < trade_date})
            if not session_dates and slice_results["daily_report_features"].rows:
                market = next((row for row in slice_results["daily_report_features"].rows
                               if row.get("symbol") == "__MARKET__"), {})
                session_dates = [str(row.get("date") or "") for row in market.get("opening_history_72", [])]
            if not session_dates:
                session_dates = [str(row.get("date") or "")[:10] for row in
                                 load_daily_history_csv(slice_results["daily_history"].rows, trade_date=trade_date)]
            relation_lag = _relation_lag_trading_days(relation_asof, trade_date, session_dates)
        except (OSError, ValueError):
            pass
    stock_names = {str(row.get("symbol") or row.get("code")): str(row.get("name") or "")
                   for row in slice_results["stock_name_map"].rows
                   if (row.get("symbol") or row.get("code")) and row.get("name")}
    stock_caps = {str(row.get("symbol") or row.get("code")): row.get("real_market_cap")
                  for row in slice_results["stock_name_map"].rows
                  if (row.get("symbol") or row.get("code")) and row.get("real_market_cap") is not None}
    td_source_manifest = {"available": src.td is not None and not src.td_error,
                          "data_lag_seconds": None, "coverage": None}
    v6_failure_internal_detail = None
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
            if asof_cutoff is not None:  # point-in-time: never read a capture taken after the report cutoff
                cutoff_tag = asof_cutoff.strftime("%H%M%S")
                hotfiles = [path for path in hotfiles if path.stem.split("_")[-1] <= cutoff_tag]
            hot_path = str(hotfiles[-1]) if hotfiles else None
        baseline_csv = slice_results["auction_baseline"].rows
        if stage == "auction" and _auction_assembly_mode() == "v6":
            facts_result = slice_results.get("auction_stock_facts")
            facts_rows = facts_result.rows if facts_result is not None else []
            facts_meta = facts_result.meta if facts_result is not None else {}
            if not facts_rows and not src.force_live_auction_facts and trade_date < date.today().isoformat():
                # Historical development previews may use event-time facts even
                # when storage availability is later than the report cutoff.
                from .data_pipeline.storage import ArtifactCatalog
                catalog = ArtifactCatalog(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data"))
                facts_path = catalog.locate("auction_stock_facts", trade_date)
                if facts_path is not None and facts_path.is_file():
                    historical_manifest = json.loads(facts_path.read_text(encoding="utf-8"))
                    if historical_manifest.get("as_of") == slice_asof:
                        facts_rows = catalog.read_rows(facts_path)
                        facts_meta = {**historical_manifest, "source": "core_data_event_time_replay"}
                        from .slices import SliceResult
                        facts_status = ((facts_meta.get("quality") or {}).get("status") or "PARTIAL")
                        facts_meta.update({"slice_id": "auction_stock_facts", "status": facts_status,
                            "asof": facts_meta["as_of"], "reason": "EVENT_TIME_REPLAY",
                            "lineage": [{"source": "core_data_event_time_replay",
                                         "used_date": trade_date,
                                         "available_at": facts_meta.get("available_at")}]})
                        slice_results["auction_stock_facts"] = SliceResult.create(facts_rows, facts_meta)
                        slice_manifest["auction_stock_facts"] = {
                            "source": "core_data_event_time_replay", "version_source": "auction_stock_facts",
                            "status": facts_status, "asof": facts_meta.get("asof"),
                            "available_at": facts_meta.get("available_at"), "rows": len(facts_rows),
                            "reason": "EVENT_TIME_REPLAY", "version": facts_meta.get("version"),
                            "quality": facts_meta.get("quality", {}), "used_date": [trade_date],
                            "coverage": facts_meta.get("quality", {}).get("coverage")}
            from .auction_v6_inputs import prepare_auction_v6_inputs
            fallback_stocks = []
            if not facts_rows:
                # Keep report.json/email observations available during slice
                # migration without invoking the legacy page or its chapters.
                fallback_stocks = _live_section_stocks(
                    src.td or _MissingTD(), trade_date=trade_date, stage=stage,
                    asof=asof or "09:26:00", relations=relations, plate_names=plate_names,
                    stock_names=stock_names, history_path=slice_results["daily_history"].rows,
                    report_features=slice_results["daily_report_features"].rows or None,
                    td_connection_factory=src.td_connection_factory)
            page = ""
            payload = prepare_auction_v6_inputs(
                trade_date=trade_date, asof=slice_asof, stock_facts=facts_rows,
                stock_names=stock_names, fallback_stocks=fallback_stocks,
                limitations=("V6 竞价事实；章节仅由已装配产品提供。" if facts_rows
                             else "竞价事实缺失；不调用旧版章节计算。"))
            payload["status"] = facts_meta.get("status", "MISSING")
        else:
            page, payload = live_pages.render_page(
                src.td or _MissingTD(), trade_date=trade_date, stage=render_stage,
                relations=relations, plate_names=plate_names, stock_names=stock_names,
                daily_history_csv=slice_results["daily_history"].rows,
                daily_report_features=slice_results["daily_report_features"].rows or None,
                auction_baseline_csv=baseline_csv, kaipan_hot_json=hot_path,
                kaipan_hot_rows=slice_results["hot_plates_today"].rows or None,
                member_snapshot_date=relation_asof, member_lag_trading_days=relation_lag,
                asof=asof_cutoff, td_connection_factory=src.td_connection_factory)
        if stage == "open":
            payload["checkpoint"] = chosen_checkpoint
            payload["asof"] = f"{trade_date}T{asof}"
        payload.setdefault("chapters", {})["reference_data"] = {
            "stock_names": {"status": "READY" if stock_names else "MISSING",
                            "reason": None if stock_names else "名称表缺失，名称显示为名称未知"},
            "daily_report_features": dict(slice_results["daily_report_features"].meta),
            "stock_caps": {"status": "READY" if stock_caps else "MISSING",
                           "reason": None if stock_caps else "市值表缺失"}}
        if stage == "auction" and _auction_assembly_mode() == "v6":
            payload["chapters"].setdefault("extremes_board", {})
        if not stock_names or not stock_caps:
            old_limitations = payload["limitations"]
            payload["limitations"] += " 参考数据：" + "；".join(
                name + "缺失" for name, available in (("名称表", bool(stock_names)), ("市值表", bool(stock_caps)))
                if not available) + "。"
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
        point = f"{trade_date}T{asof or '09:45:30'}"
        live_stocks = payload.get("stocks") if isinstance(payload.get("stocks"), list) else []
        feedback_symbols: set[str] = set()
        if not live_stocks and not (stage == "auction" and _auction_assembly_mode() == "v6"):
            from .limit_reason_theme import _pool_rows, _pool_values
            feedback_symbols = {symbol for raw in _pool_rows(slice_results["yest_limit_pool"].rows)
                                if (symbol := _pool_values(raw)[0])}
            live_stocks = _live_section_stocks(
                src.td or _MissingTD(), trade_date=trade_date, stage=stage,
                asof=asof or ("09:26:00" if stage == "auction" else "09:46:00"),
                relations=relations, plate_names=plate_names, stock_names=stock_names,
        history_path=slice_results["daily_history"].rows, plate_metadata=plate_metadata,
        report_features=slice_results["daily_report_features"].rows or None,
        path_symbols=feedback_symbols,
                checkpoint=chosen_checkpoint,
        td_connection_factory=src.td_connection_factory)
        _attach_range_positions(live_stocks, slice_results["daily_history"].rows, trade_date,
                                report_features=slice_results["daily_report_features"].rows or None)
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
        if stage == "auction" and _auction_assembly_mode() == "v6" and not highboards:
            from .limit_reason_theme import _pool_rows
            highboards = _pool_rows(slice_results["yest_limit_pool"].rows)
        stock_by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row
                           for row in live_stocks if isinstance(row, Mapping)}
        for row in highboards if isinstance(highboards, list) else []:
            if isinstance(row, dict):
                symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
                row["path_type"] = path_labels.get(symbol, "UNKNOWN")
                live = stock_by_symbol.get(symbol, {})
                row.setdefault("0925", live.get("p0925_pct"))
                row.setdefault("0932", live.get("p0932_pct"))
                row["0938"] = live.get("p0938_pct")
                row["0945"] = live.get("p0945_pct")
                row["current_checkpoint"] = chosen_checkpoint if stage == "open" else "0925"
                row["current_pct"] = (live.get("current_pct", live.get(f"p{chosen_checkpoint}_pct")) if stage == "open"
                                       else live.get("p0925_pct"))
                row["current_amount_yuan"] = (live.get("current_amount_yuan") if stage == "open"
                                               else live.get("p0925_amount_yuan"))
                row["price"] = (live.get("price") if stage == "open" else
                                live.get("price") or live.get("p0925_price"))
                row["pre_close"] = live.get("pre_close") or live.get("p0925_pre_close")
                row["match_amount_yuan"] = live.get("auction_amount_yuan")
        if stage == "auction" and _auction_assembly_mode() == "v6":
            payload["chapters"]["yesterday_highboards"] = highboards
            payload["chapters"]["yesterday_limit_pool"] = highboards
        payload["ladder_section"] = build_ladder_section(
            highboards if isinstance(highboards, list) else [], stage=stage, asof=point,
            frequencies=path_frequencies, market_stocks=live_stocks,
            checkpoint=chosen_checkpoint if stage == "open" else "0925")
        # Build the story from the same live payload used by the appendices. Keep
        # the auction terminal as an explicit handoff in open reports.
        from .story_features import build_live_story_features
        from .story_composer import compose_story
        from .tradability_funnel import filter_candidates
        story_cutoff = asof or ("09:26:00" if stage == "auction" else "09:46:00")
        features = build_live_story_features(payload, stage=stage, cutoff=story_cutoff,
                                             observed_at=point)
        funnel_candidates = []
        for group in payload.get("a_to_b", []) if isinstance(payload.get("a_to_b"), list) else []:
            if not isinstance(group, Mapping):
                continue
            candidates = group.get("b_candidates", group.get("candidates", []))
            for candidate in candidates if isinstance(candidates, list) else []:
                if not isinstance(candidate, Mapping):
                    continue
                metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), Mapping) else {}
                funnel_candidates.append({**candidate,
                    "symbol": candidate.get("symbol") or candidate.get("code") or candidate.get("stock_code"),
                    "bucket": candidate.get("bucket", "wait"),
                    "auction_price_status": candidate.get("auction_price_status", "UNKNOWN"),
                    "auction_pct": metrics.get("auction_pct"),
                    f"p{chosen_checkpoint or '0925'}_pct": metrics.get(f"p{chosen_checkpoint}_pct", metrics.get("current_pct")) if stage == "open" else metrics.get("auction_pct"),
                    "current_pct": metrics.get("current_pct"),
                    f"relative_to_plate_{chosen_checkpoint or '0925'}_pp": metrics.get(f"relative_to_plate_{chosen_checkpoint}_pp") if stage == "open" else None,
                    "position20_prior": metrics.get("position20_prior", metrics.get("position20")),
                    "reasons": candidate.get("reasons", []),
                    "origin": "A_TO_B"})
        # Yesterday's highboard leaders are valid observation objects in their
        # own right; retain them alongside B peers when they have an auction row.
        known_symbols = {str(row.get("symbol", "")).zfill(6) for row in funnel_candidates}
        highboards = payload.get("chapters", {}).get("yesterday_highboards", []) if isinstance(payload.get("chapters"), Mapping) else []
        for row in highboards if isinstance(highboards, list) else []:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
            auction = next((r for r in live_stocks if isinstance(r, Mapping) and
                            str(r.get("symbol", "")).zfill(6) == symbol), {})
            if not symbol or symbol in known_symbols or not auction:
                continue
            pct = auction.get("auction_pct", auction.get("p0925_pct"))
            current_pct = (auction.get(f"p{chosen_checkpoint}_pct") if stage == "open"
                           else auction.get("p0925_pct"))
            funnel_candidates.append({"symbol": symbol, "name": row.get("name", symbol),
                "bucket": "wait", "selection_reason": "昨日高标，跟踪竞价承接",
                "condition": ((f"竞价{float(pct):+.1f}%，开盘确认仍封住" if pct is not None else "")
                              if auction.get("auction_limit_state") in (1, "1") else
                              (f"竞价{float(pct):+.1f}%，看0932仍≥{5 if pct is not None and pct >= 5 else 0}%且不弱于板块"
                               if stage == "auction" and pct is not None else
                              (f"竞价{float(pct):+.1f}%后{chosen_checkpoint}{float(current_pct):+.1f}%，看后续保持红盘且不弱于板块"
                                if stage == "open" and pct is not None and current_pct is not None else ""))),
                "risk": (f"竞价涨幅{float(pct):+.1f}%，需复核承接" if pct is not None else ""),
                "auction_price_status": auction.get("auction_price_status", "UNKNOWN"),
                "auction_pct": pct, "match_amount_yuan": auction.get("auction_amount_yuan"),
                "current_checkpoint": chosen_checkpoint if stage == "open" else "0925",
                "current_pct": current_pct,
                f"p{chosen_checkpoint or '0925'}_pct": current_pct,
                "auction_limit_state": auction.get("auction_limit_state"), "origin": "YESTERDAY_HIGHBOARD"})
            known_symbols.add(symbol)
            if len(funnel_candidates) >= 30:
                break
        story_funnel = filter_candidates(funnel_candidates, features, asof=point,
                                         checkpoint=chosen_checkpoint if stage == "open" else None)
        previous_acts = []
        if stage == "open" and isinstance(payload.get("handoff"), Mapping):
            handoff = payload["handoff"].get("auction_0925")
            terminal = handoff.get("final_0925") if isinstance(handoff, Mapping) else None
            if isinstance(terminal, Mapping):
                prior_payload = {"trade_date": trade_date, "auction_terminal_0925": terminal}
                prior_features = build_live_story_features(prior_payload, stage="auction",
                                                           cutoff="09:25:06")
                prior_story = compose_story(prior_features, [], {"shown": {}}, previous_acts=[])
                previous_acts = [act for act in prior_story.get("acts", [])
                                 if act.get("stage") == "auction"]
        payload["story"] = compose_story(features, [], story_funnel,
                                         previous_acts=previous_acts)
        # A compact auction handoff may reuse only history available before this
        # trade date. Emit fewer than three events when the data do not support more.
        terminal_for_events = payload.get("auction_terminal_0925")
        if stage == "open" and isinstance(payload.get("handoff"), Mapping):
            auction_ref = payload["handoff"].get("auction_0925")
            terminal_for_events = auction_ref.get("final_0925") if isinstance(auction_ref, Mapping) else None
        history_rows = payload.get("history", {}).get("daily", []) if isinstance(payload.get("history"), Mapping) else []
        if not history_rows and slice_results["daily_history"].rows:
            try:
                import statistics
                from .live_session import load_daily_history_csv
                historical = load_daily_history_csv(slice_results["daily_history"].rows, trade_date=trade_date)
                by_day: dict[str, list[float]] = {}
                for row in historical:
                    day = str(row.get("date") or "")
                    value = row.get("pct_chg")
                    if day < trade_date and isinstance(value, (int, float)):
                        by_day.setdefault(day, []).append(float(value))
                history_rows = [{
                    "date": day,
                    "median_open_pct": statistics.median(values),
                    "up_share_pct": 100.0 * sum(value > 0 for value in values) / len(values),
                } for day, values in sorted(by_day.items())[-64:] if values]
            except (OSError, ValueError, TypeError):
                history_rows = []
        terminal_usable = (isinstance(terminal_for_events, Mapping)
                           and terminal_for_events.get("status") not in {"STALE", "NOT_READY", "NOT_YET_AVAILABLE"}
                           and terminal_for_events.get("symbol_count") != 0)
        if terminal_usable and isinstance(history_rows, list):
            events = []
            amount = terminal_for_events.get("market_match_amount_yuan")
            baseline = terminal_for_events.get("auction_baseline_comparison")
            if isinstance(amount, (int, float)) and isinstance(baseline, Mapping):
                multiple, percentile = baseline.get("multiple"), baseline.get("percentile")
                previous = terminal_for_events.get("vs_previous_day_final")
                if not isinstance(previous, (int, float)) and slice_results["auction_baseline"].rows:
                    try:
                        prior_amounts = [(str(row.get("date") or "")[:10], float(row["sum_yi"]))
                                         for row in slice_results["auction_baseline"].rows
                                         if str(row.get("date") or "")[:10] < trade_date
                                         and row.get("sum_yi") not in (None, "")]
                        if prior_amounts:
                            previous = (amount / 1e8) / max(prior_amounts)[1] if max(prior_amounts)[1] else None
                    except (OSError, ValueError, TypeError):
                        previous = None
                prev_text = f"，约为昨日终值{float(previous):.2f}倍" if isinstance(previous, (int, float)) else ""
                if isinstance(multiple, (int, float)) and isinstance(percentile, (int, float)):
                    baseline_yi = baseline.get("median_yi")
                    summary = (f"竞价额{amount / 1e8:.1f}亿{prev_text}；64日同刻基线"
                               f"{float(baseline_yi):.1f}亿，{multiple:.2f}倍、"
                               f"第{percentile:.0f}分位，不算异常放量。")
                    events.append({"id": "auction_amount_context", "metric": "auction_amount_yuan",
                                   "display_label": "对照观察", "summary": summary})
            breadth_parts = []
            for metric, current_key, history_key, label in (
                ("auction_median_pct", "median_pct_change", "median_open_pct", "竞价中位数"),
                ("auction_up_share", "up_share_pct", "up_share_pct", "竞价上涨占比"),
            ):
                current = terminal_for_events.get(current_key)
                values = [row.get(history_key) for row in history_rows if isinstance(row, Mapping)
                          and row.get("date") and str(row.get("date")) < trade_date
                          and isinstance(row.get(history_key), (int, float))]
                if isinstance(current, (int, float)) and len(values) >= 20:
                    rank = 1 + sum(value > current for value in values)
                    breadth_parts.append(f"{label}{current:.2f}{'%' if metric == 'auction_up_share' else '个百分点'}，近{len(values)}日由高到低第{rank}/{len(values)}")
            if breadth_parts:
                import statistics
                yesterday_close = None
                if slice_results["daily_history"].rows:
                    from .live_session import load_daily_history_csv
                    raw_daily = load_daily_history_csv(slice_results["daily_history"].rows, trade_date=trade_date)
                    prior_days = sorted({str(row.get("date") or "") for row in raw_daily
                                         if str(row.get("date") or "") < trade_date})
                    if prior_days:
                        last_day = prior_days[-1]
                        returns = [row.get("pct_chg") for row in raw_daily
                                   if row.get("date") == last_day and isinstance(row.get("pct_chg"), (int, float))]
                        if returns:
                            yesterday_close = (statistics.median(returns),
                                100.0 * sum(value > 0 for value in returns) / len(returns), last_day)
                if yesterday_close:
                    if isinstance(terminal_for_events.get("median_pct_change"), (int, float)) and isinstance(
                            terminal_for_events.get("up_share_pct"), (int, float)):
                        transition = "冰点反转：" if yesterday_close[0] < 0 < float(terminal_for_events["median_pct_change"]) else "昨日→今日："
                        breadth_parts.insert(0, (f"{transition}昨日收盘中位{yesterday_close[0]:+.1f}%/上涨"
                                                 f"{yesterday_close[1]:.1f}%→今日竞价"
                                                 f"{float(terminal_for_events['median_pct_change']):+.1f}%/"
                                                 f"{float(terminal_for_events['up_share_pct']):.1f}%"
                                                 f"（昨日{yesterday_close[2]}）"))
                    else:
                        breadth_parts.insert(0, f"昨日收盘中位{yesterday_close[0]:+.1f}%/上涨{yesterday_close[1]:.1f}%")
                events.append({"id": "auction_breadth_context", "metric": "auction_up_share",
                               "display_label": "历史异常" if any("第1/" in part for part in breadth_parts) else "对照观察",
                               "summary": "；".join(breadth_parts) + "。"})
            if stage == "auction":
                highboards = payload.get("chapters", {}).get("yesterday_highboards", [])
                summary_data = payload.get("chapters", {}).get("yesterday_highboards_summary", {})
                pool = summary_data.get("yesterday_limit_pool", {}) if isinstance(summary_data, Mapping) else {}
                ladder_parts = []
                if isinstance(pool, Mapping) and isinstance(pool.get("mean_pct_change"), (int, float)):
                    ladder_parts.append(f"昨日涨停池竞价均值{pool['mean_pct_change']:+.1f}%")
                rows = highboards if isinstance(highboards, list) else []
                four = [r for r in rows if isinstance(r, Mapping) and r.get("prior_streak", r.get("streak")) == 4]
                three = [r for r in rows if isinstance(r, Mapping) and r.get("prior_streak", r.get("streak")) == 3]
                if four:
                    opened = sum(isinstance(r.get("0925"), (int, float)) and r["0925"] > 0 for r in four)
                    sealed = sum(r.get("status") == "封" for r in four)
                    ladder_parts.append(f"昨4板{len(four)}只：{opened}只高开、{sealed}只封停价")
                if three:
                    weakest = min(three, key=lambda r: r.get("0925") if isinstance(r.get("0925"), (int, float)) else 999)
                    if isinstance(weakest.get("0925"), (int, float)):
                        ladder_parts.append(f"昨3板最弱{weakest.get('name', weakest.get('symbol'))} {weakest['0925']:+.1f}%")
                snapshots = payload.get("snapshots", [])
                if isinstance(snapshots, list):
                    cuts = {r.get("label"): r.get("top_plates_by_share", []) for r in snapshots if isinstance(r, Mapping)}
                    old = {r.get("code"): r for r in cuts.get("0924", []) if isinstance(r, Mapping)}
                    latest = [r for r in cuts.get("0925", []) if isinstance(r, Mapping)]
                    changes = [(r, r.get("amount_share_bp", 0) - old[r.get("code")].get("amount_share_bp", 0))
                               for r in latest if r.get("code") in old]
                    if changes and max(abs(pair[1]) for pair in changes) >= 20:
                        plate, delta = max(changes, key=lambda pair: abs(pair[1]))
                        ladder_parts.append(f"板块份额变化最大：{plate.get('name')} {delta:+.0f}bp")
                if ladder_parts:
                    events.append({"id": "auction_ladder_plate_context", "metric": "auction_up_share",
                                   "display_label": "对照观察", "summary": "；".join(ladder_parts) + "。"})
            payload["story"]["top_events"] = events[:3]
        from .ladder_cluster import build_front_page_data
        from .limit_reason_theme import (THEME_SNAPSHOT_NODE_SCHEMA,
                                         load_kaipan_theme_snapshot,
                                         merge_theme_snapshot_static_fallback,
                                         theme_snapshot_node_inputs,
                                         theme_snapshot_version)
        from .data_pipeline.node_store import NodeStore
        from .data_pipeline.storage import ArtifactCatalog
        calendar_days = sorted({str(row.get("date") or "")[:10] for row in slice_results["trading_calendar"].rows
                                if str(row.get("date") or "")[:10] < trade_date})
        history_days = sorted({str(row.get("date") or "")[:10] for row in slice_results["daily_history"].rows
                               if str(row.get("date") or "")[:10] < trade_date})
        previous_trade_date = max(calendar_days or history_days, default=None)
        static_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for relation in slice_results["plate_members"].rows:
            symbol = str(relation.get("symbol", relation.get("stock_code", relation.get("stockid", ""))) or "")[-6:].zfill(6)
            code = str(relation.get("plate_code", relation.get("plateid", relation.get("code", ""))) or "").strip()
            name = str(plate_names.get(code) or "").strip()
            if symbol and code and name:
                static_by_symbol.setdefault(symbol, []).append({"id": code, "name": name})
        def node_slice_version(name: str) -> str:
            return str(slice_results[name].meta.get("version") or "unknown")
        reason_lineage = [
            (str(item.get("trade_date") or ""), str(item.get("version") or "unknown"))
            for item in slice_results["limit_reason_history"].meta.get("lineage", [])
            if item.get("trade_date")]
        node_inputs = theme_snapshot_node_inputs(
            trade_date=trade_date, previous_trade_date=previous_trade_date or "",
            slices={"yest_limit_pool": (node_slice_version("yest_limit_pool"),
                                        len(slice_results["yest_limit_pool"].rows)),
                    "plate_registry": (node_slice_version("plate_registry"),
                                       len(slice_results["plate_registry"].rows)),
                    "plate_members": (node_slice_version("plate_members"),
                                      len(slice_results["plate_members"].rows)),
                    "hot_plates_yesterday": (node_slice_version("hot_plates_yesterday"),
                                             len(slice_results["hot_plates_yesterday"].rows)),
                    "limit_reason_history": (json.dumps(reason_lineage, separators=(",", ":")),
                                             len(slice_results["limit_reason_history"].rows))},
            calendar_dates=calendar_days, plate_names=plate_names)
        node_input_version = theme_snapshot_version(node_inputs)
        catalog = ArtifactCatalog(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data"))
        theme_snapshot, snapshot_hit = NodeStore(catalog).compute(
            "theme_snapshot", trade_date, f"{THEME_SNAPSHOT_NODE_SCHEMA}:algorithm-v1", node_inputs,
            lambda: load_kaipan_theme_snapshot(
                None, report_date=trade_date, previous_trade_date=previous_trade_date or "",
                observed_at=point, plate_names=plate_names,
                static_themes_by_symbol=static_by_symbol,
                pool_rows=slice_results["yest_limit_pool"].rows,
                reason_rows=slice_results["limit_reason_history"].rows,
                hot_rows=slice_results["hot_plates_yesterday"].rows,
                trading_dates=calendar_days), asof=slice_asof)
        if snapshot_hit:
            theme_snapshot = merge_theme_snapshot_static_fallback(
                theme_snapshot, static_by_symbol,
                expected_previous_date=previous_trade_date or "",
                plate_names=plate_names, trading_dates=calendar_days)
        else:
            degraded.append("THEME_SNAPSHOT_NODE_MISS_COMPUTED")
        # D06 is the one primary-theme authority for ladder, A→B and tracking.
        from .stock_theme_assignment import build_stock_theme_assignments
        from .limit_reason_theme import _pool_rows, _pool_values
        pool_labels: dict[str, list[dict[str, str]]] = {}
        for raw in _pool_rows(slice_results["yest_limit_pool"].rows):
            symbol, label, topics, code, _tier = _pool_values(raw)
            candidates = ([{"id": code, "name": str(plate_names.get(code) or label or code)}]
                          if code else [])
            candidates.extend({"id": name, "name": name} for name in topics if name)
            if symbol and candidates:
                pool_labels[symbol] = candidates
        pit_members: dict[str, set[str]] = {}
        for relation in slice_results["plate_members"].rows:
            symbol = str(relation.get("symbol", relation.get("stock_code",
                          relation.get("stockid", relation.get("code", "")))) or "")[-6:].zfill(6)
            code = str(relation.get("plate_code", relation.get("plateid",
                        relation.get("code_plate", relation.get("plate", "")))) or "").strip()
            if symbol and code:
                pit_members.setdefault(symbol, set()).add(code)
        hot_by_code = {}
        for row in slice_results["hot_plates_yesterday"].rows:
            code = str(row.get("plate_code", row.get("code", "")) or "").strip()
            name = str(row.get("name", row.get("plate_name", "")) or "").strip()
            if code and name:
                hot_by_code[code] = {"id": code, "name": name}
        hot_by_symbol = {symbol: [hot_by_code[code] for code in sorted(codes) if code in hot_by_code]
                         for symbol, codes in pit_members.items()}
        reason_rows = []
        for row in slice_results["limit_reason_history"].rows:
            normalized = dict(row)
            codes = row.get("ZSCode", row.get("zs_code", []))
            if isinstance(codes, str):
                codes = [codes]
            if isinstance(codes, list):
                normalized["ZSCode"] = [{"id": str(code), "name": str(plate_names.get(str(code), str(code)))}
                                        for code in codes if str(code).strip()]
            reason_rows.append(normalized)
        d06_slice = slice_results.get("stock_theme_assignment")
        persisted_assignments = (_d06_persisted_assignments(
            d06_slice, trade_date=trade_date, used_date=previous_trade_date or "",
            asof=slice_asof, input_version=node_input_version,
            visibility_sha256=node_input_version) if previous_trade_date else {})
        stock_assignment = persisted_assignments or build_stock_theme_assignments(
            trade_date=trade_date, previous_trade_date=previous_trade_date or "",
            pool_labels=pool_labels, reason_rows=reason_rows, static_themes=static_by_symbol,
            hot_plate_names=hot_by_symbol,
            members={symbol: sorted(codes | {item["id"] for item in pool_labels.get(symbol, [])})
                     for symbol, codes in pit_members.items()} |
                    {symbol: [item["id"] for item in candidates]
                     for symbol, candidates in pool_labels.items()},
            trading_dates=calendar_days)
        # D09/D10: use the complete visible previous-day limit pool and same-cutoff
        # quote fields. Missing per-checkpoint evidence stays UNKNOWN.
        from .streak_feedback import build_streak_feedback, persist_streak_feedback
        feedback_pool = []
        for raw in _pool_rows(slice_results["yest_limit_pool"].rows):
            symbol, _label, _topics, _code, _tier = _pool_values(raw)
            level = raw.get("lb_days", raw.get("streak")) if isinstance(raw, Mapping) else (
                raw[15] if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) > 15 else None)
            try:
                level = int(level)
            except (TypeError, ValueError, OverflowError):
                level = None
            if symbol and level and level > 0:
                feedback_pool.append({"symbol": symbol, "name": stock_names.get(symbol, symbol), "lb_days": level})
        feedback_snapshots: dict[str, dict[str, dict[str, Any]]] = {}
        from .live_session import OPEN_CHECKPOINTS
        checkpoint_field = {tag: tag for tag, _ in OPEN_CHECKPOINTS}
        for checkpoint, suffix in checkpoint_field.items():
            snapshot_rows = {}
            checkpoint_time = dict(OPEN_CHECKPOINTS)[checkpoint]
            for stock in live_stocks:
                if not isinstance(stock, Mapping):
                    continue
                symbol = str(stock.get("symbol", stock.get("code", ""))).zfill(6)
                if not symbol:
                    continue
                snapshot_rows[symbol] = {
                    "price": stock.get(f"p{suffix}_price"),
                    "pre_close": stock.get(f"p{suffix}_pre_close"),
                    "rest_bid": stock.get(f"p{suffix}_rest_bid", stock.get("rest_bid_yuan") if checkpoint == "0925" else None),
                    "rest_ask": stock.get(f"p{suffix}_rest_ask"),
                    "limit_state": stock.get(f"p{suffix}_limit_state", stock.get("auction_limit_state") if checkpoint == "0925" else None),
                    "high": stock.get(f"p{suffix}_high"),
                    "prior_seal_observed": any(
                        str(stamp) <= f"{trade_date} {checkpoint_time}"
                        for stamp in stock.get("observed_seal_timestamps", [])
                    ),
                    "source_ts": stock.get(f"p{suffix}_source_ts"),
                    "return_pct": stock.get(f"p{suffix}_pct", stock.get("auction_pct") if checkpoint == "0925" else None),
                    "amount": stock.get(f"p{suffix}_amount", stock.get("auction_amount_yuan") if checkpoint == "0925" else None),
                }
            feedback_snapshots[checkpoint] = snapshot_rows
        feedback = build_streak_feedback(
            trade_date=trade_date, used_date=previous_trade_date or "UNKNOWN",
            version=slice_results["yest_limit_pool"].meta.get("version"),
            pool=feedback_pool, snapshots=feedback_snapshots, stage=stage,
            checkpoint=chosen_checkpoint if stage == "open" else None,
            theme_assignments=stock_assignment)
        payload["streak_feedback"] = feedback
        try:
            persist_streak_feedback(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data"), feedback)
        except OSError:
            degraded.append("STREAK_FEEDBACK_COLD_WRITE_FAILED")
        active_checkpoint = chosen_checkpoint if stage == "open" else "0925"
        active_matrix = feedback["checkpoints"].get(active_checkpoint, {}).get("matrix", {})
        active_levels = active_matrix.get("by_level", {})
        ladder = payload.get("ladder_section")
        if isinstance(ladder, dict):
            ladder["promotion_checkpoint"] = active_checkpoint
            ladder["promotion_by_previous_level"] = active_levels
            active_rows = feedback["checkpoints"].get(active_checkpoint, {}).get("rows", [])
            active_evaluable = any(row.get("status") != "UNKNOWN" for row in active_rows)
            ladder["promotion_status"] = (
                "MISSING_YESTERDAY_POOL" if not feedback_pool else
                "READY" if active_evaluable else "UNKNOWN")
            ladder["promotion_used_date"] = previous_trade_date or "UNKNOWN"
            ladder["promotion_version"] = feedback.get("content_version")
        for symbol, assignment in stock_assignment.items():
            primary = assignment.get("primary_theme")
            snapshot_row = theme_snapshot.setdefault("assignments", {}).setdefault(symbol, {})
            snapshot_row.update({"theme_key": primary.get("id") if primary else "NONE",
                                 "theme_name": primary.get("name") if primary else "无题材",
                                 "theme_source": assignment["source"],
                                 "secondary_themes": [{"code": row["id"], "name": row["name"]}
                                                      for row in assignment["secondary_themes"]],
                                 "primary_theme": primary, "source": assignment["source"],
                                 "as_of": assignment["as_of"], "version": assignment["version"]})
        # Rebuild the displayed plate linkage from the same primary assignments.
        current_checkpoint = chosen_checkpoint if stage == "open" else "0925"
        current_label = f"p{current_checkpoint}"
        linkage_states = {}
        for stock in live_stocks:
            symbol = str(stock.get("symbol", stock.get("code", ""))).zfill(6)
            if symbol not in stock_assignment:
                continue
            linkage_states[symbol] = {
                "pct_change": stock.get(f"{current_label}_pct"),
                "match_amount_yuan": stock.get(f"{current_label}_amount_yuan",
                                                stock.get(f"{current_label}_amount",
                                                          stock.get("auction_amount_yuan"))),
                "limit_state": stock.get("auction_limit_state" if stage != "open" else "open_limit_state"),
            }
        class_by_code = {}
        if linkage_states:
            from .plate_linkage import build_plate_linkage
            linkage = build_plate_linkage(
                linkage_states, {str(code): set(members) for code, members in relations.items()}, plate_names,
                stock_names=stock_names,
                theme_assignments=stock_assignment, min_members=8, top_n=None)
            existing = payload.get("chapters", {}).get("plate_linkage", {})
            if isinstance(existing, Mapping):
                payload["chapters"]["plate_linkage"] = {
                    **existing, "available": bool(linkage["rows"]),
                    "slice_label": current_checkpoint,
                    "rows": linkage["rows"][:15],
                    "primary_theme_rows": (linkage.get("primary_theme_rows") or [])[:15],
                    "down_limit_top5": linkage["down_limit_top5"],
                    "changes": None,
                }
        # D13/D14/D15: one table uses the exact same primary-theme assignments
        # and active quote checkpoint as linkage. Previous-day rows are accepted
        # only from an earlier report at the same stage/checkpoint.
        if linkage_states:
            from .plate_cross_table import build_plate_cross_table
            # Cross-plate aggregation uses the full point-in-time membership
            # slice. Primary-theme assignments remain specific to promotion
            # feedback and must not erase overlapping plate membership here.
            current_relations = {str(code): set(members) for code, members in relations.items()}
            current_names = dict(plate_names)
            pool_by_symbol = {}
            for raw in _pool_rows(slice_results["yest_limit_pool"].rows):
                symbol, _label, _topics, _code, _tier = _pool_values(raw)
                if symbol:
                    pool_by_symbol[symbol] = raw
            reason_by_symbol = {}
            for reason in reason_rows:
                symbol = str(reason.get("StockID", reason.get("stock_id", reason.get("symbol", ""))))
                if symbol:
                    reason_by_symbol[symbol[-6:].zfill(6)] = reason
            table_states = {}
            for stock in live_stocks:
                if not isinstance(stock, Mapping):
                    continue
                symbol = str(stock.get("symbol", stock.get("code", ""))).zfill(6)
                if symbol not in stock_assignment:
                    continue
                raw_pool = pool_by_symbol.get(symbol, {})
                pool_map = raw_pool if isinstance(raw_pool, Mapping) else {}
                reason = reason_by_symbol.get(symbol, {})
                table_states[symbol] = {
                    "name": stock_names.get(symbol, stock.get("name") or symbol),
                    "pct_change": stock.get(f"{current_label}_pct"),
                    "amount_yuan": stock.get(f"{current_label}_amount_yuan",
                                              stock.get(f"{current_label}_amount")),
                    "limit_state": stock.get("auction_limit_state" if stage != "open" else "open_limit_state"),
                    "sealed": stock.get("close_seal_up"),
                    "lb_days": (pool_map.get("lb_days", pool_map.get("streak"))
                                if isinstance(pool_map, Mapping) else None),
                    "seal_time": stock.get("seal_time", stock.get("first_seal_time")),
                    "reason_sclt": reason.get("SCLT", reason.get("sclt")),
                }
            previous_rows = None
            previous_path = _previous_report(
                Path(refs.get("prev_reports_root") or out_root), trade_date,
                (STAGE_NAMES[stage],))
            if previous_path:
                try:
                    previous_payload = json.loads(previous_path.read_text(encoding="utf-8"))
                    previous_chapters = previous_payload.get("chapters", {})
                    previous_product = previous_chapters.get("plate_cross_table", {})
                    previous_rows = (previous_product.get("rows") if isinstance(previous_product, Mapping)
                                     else None)
                    if previous_rows is None:
                        old_linkage = previous_chapters.get("plate_linkage", {})
                        expected_label = current_checkpoint
                        if (isinstance(old_linkage, Mapping)
                                and str(old_linkage.get("slice_label") or "") == expected_label):
                            previous_rows = [
                                {"code": row.get("code"),
                                 "equal_weight_pct": row.get("equal_weight_pct", row.get("equal_weight_mean_pct"))}
                                for row in old_linkage.get("rows", []) if isinstance(row, Mapping)
                            ]
                except (OSError, ValueError, TypeError):
                    previous_rows = None
            market_total = None
            market_volume = payload.get("chapters", {}).get("market_price_volume", {})
            if isinstance(market_volume, Mapping):
                market_total = market_volume.get("market_match_amount_yuan")
            active_theme_matrix = feedback["checkpoints"].get(active_checkpoint, {}).get("matrix", {})
            theme_stats = active_theme_matrix.get("by_theme", {})
            registry_by_code = {str(row.get("code") or row.get("plate_code") or row.get("plateid") or ""): row
                                for row in registry_rows if isinstance(row, Mapping)}
            promotion_by_theme = {}
            promotion_crosswalk = {"matched": {}, "unmatched_theme_ids": []}
            for theme_id, stats in theme_stats.items():
                theme_name = str(stats.get("theme") or "") if isinstance(stats, Mapping) else ""
                candidates = []
                for code in current_relations:
                    entry = registry_by_code.get(code, {})
                    aliases = entry.get("aliases", [])
                    aliases = aliases if isinstance(aliases, list) else []
                    known_ids = {code, str(entry.get("name") or ""), *map(str, aliases)}
                    if str(theme_id) in known_ids or (theme_name and theme_name in known_ids):
                        candidates.append(code)
                if len(candidates) == 1:
                    code = candidates[0]
                    promotion_by_theme[code] = stats
                    promotion_crosswalk["matched"][str(theme_id)] = code
                else:
                    promotion_crosswalk["unmatched_theme_ids"].append(str(theme_id))
            class_by_code = {}
            for code, members in current_relations.items():
                registry_row = registry_by_code.get(code, {})
                count = len(members)
                children = registry_row.get("children", [])
                class_by_code[code] = ("BROAD" if count >= 300 or
                                       (isinstance(children, list) and bool(children) and count >= 150)
                                       else "NICHE" if count <= 15 else "MID")
            cross_rows = build_plate_cross_table(
                states=table_states, relations=current_relations, plate_names=current_names,
                market_amount_yuan=market_total, previous_rows=previous_rows,
                promotion_by_theme=promotion_by_theme, class_by_code=class_by_code,
                historical_by_code={str(row.get("plate_code")): {
                    "median_yuan": row.get("median_yuan"), "amounts_yuan": row.get("amounts_yuan", []),
                    "sample_days": row.get("sample_days", 0), "slot": row.get("slot"),
                    "version": slice_results["plate_amount_baseline"].meta.get("version"),
                    "used_date": slice_results["plate_amount_baseline"].meta.get("pit_date") or
                                 slice_results["plate_amount_baseline"].meta.get("trade_date")}
                    for row in slice_results["plate_amount_baseline"].rows
                    if row.get("slot") == current_checkpoint})
            broad_codes = {code for code, label in class_by_code.items() if label == "BROAD"}
            broad_symbols = set().union(*(set(current_relations.get(code, ())) for code in broad_codes)) if broad_codes else set()
            broad_facts = [table_states[symbol] for symbol in sorted(broad_symbols)
                           if isinstance(table_states.get(symbol), Mapping)]
            broad_amounts = [value for row in broad_facts
                             if (value := _numeric(row.get("amount_yuan", row.get("match_amount_yuan")))) is not None
                             and value >= 0]
            broad_returns = [value for row in broad_facts
                             if (value := _numeric(row.get("pct_change"))) is not None]
            broad_amount = sum(broad_amounts) if len(broad_amounts) == len(broad_symbols) and broad_symbols else None
            chapters = payload.setdefault("chapters", {})
            chapters["plate_cross_table"] = {
                "checkpoint": active_checkpoint,
                "members_version": slice_results["plate_members"].meta.get("version"),
                "members_asof": relation_asof,
                "comparison_label": ("昨日同截点" if previous_rows is not None else "昨日同截点 UNKNOWN"),
                "previous_report": str(previous_path) if previous_path else None,
                "amount_basis": "成员合计额；成员重叠重复计入，不是守恒分摊额",
                "rows": cross_rows[:15],
                "main_rows": [row for row in cross_rows if row.get("class") != "BROAD"][:10],
                "broad_summary": {
                    "count": len(broad_codes),
                    "distinct_stock_count": len(broad_facts),
                    "distinct_market_share_pct": (100.0 * broad_amount / market_total
                        if broad_amount is not None and market_total is not None and market_total > 0 else None),
                    "equal_weight_pct_mean": sum(broad_returns) / len(broad_returns) if broad_returns else None,
                    "names": [current_names.get(code, code) for code in sorted(broad_codes)],
                },
                "promotion_theme_crosswalk": promotion_crosswalk,
            }
            requested_codes = set(current_relations)
            requested_codes.update(str(row.get("code") or row.get("plate_code") or "")
                                    for row in slice_results["hot_plates_today"].rows[:50])
            dictionary_rows = []
            for code in sorted(requested_codes - {""}):
                row = registry_by_code.get(code, {})
                name = str(row.get("name") or current_names.get(code) or plate_names.get(code) or code)
                if code == "801827" and name == code:
                    name = "AI视频"
                try:
                    registry_size = int(row.get("size", row.get("members")))
                except (TypeError, ValueError, OverflowError):
                    registry_size = None
                member_count = (len(current_relations[code]) if code in current_relations
                                else registry_size)
                raw_class = str(row.get("class", row.get("kind", "UNKNOWN"))).upper()
                children = row.get("children", [])
                if member_count is None:
                    plate_class = "UNKNOWN"
                elif member_count >= 300 or (isinstance(children, list) and children and member_count >= 150):
                    plate_class = "BROAD"
                elif member_count <= 15:
                    plate_class = "NICHE"
                elif raw_class in {"EVENT", "GENERIC", "GENERIC_FACTOR", "NOISE"}:
                    plate_class = raw_class
                else:
                    plate_class = "THEME"
                dictionary_rows.append({
                    "code": code, "name": name,
                    "class": plate_class, "member_count": member_count,
                    "aliases": row.get("aliases", ["AI应用(AI视频)"] if code == "801827" else []),
                    "parent": row.get("parent"), "children": row.get("children", []),
                })
            payload["plate_dictionary"] = {
                "registry_version": slice_results["plate_registry"].meta.get("version"),
                "members_version": slice_results["plate_members"].meta.get("version"),
                "members_asof": relation_asof,
                "source": "PIT_SLICE",
                "rows": dictionary_rows,
            }
            linkage_product = chapters.get("plate_linkage")
            if isinstance(linkage_product, dict):
                linkage_product["cross_table_rows"] = cross_rows[:15]
            leader_by_name = {row["name"]: row["leaders"].get("amount_max")
                              for row in cross_rows if row.get("leaders", {}).get("amount_max")}
            ladder = payload.get("front_page", {}).get("ladder", {})
            for cluster in ladder.get("clusters", []) if isinstance(ladder, Mapping) else []:
                if not isinstance(cluster, dict) or cluster.get("leader"):
                    continue
                leader = leader_by_name.get(str(cluster.get("name") or ""))
                if leader:
                    cluster["leader"] = {"symbol": leader["symbol"], "name": leader["name"],
                                          "basis": "AMOUNT_MAX"}
        # Tracking extremes are rendered from the live page built earlier in this
        # pipeline; overwrite their independently selected main plate in place.
        def assign_extreme_rows(value: Any) -> None:
            if isinstance(value, dict):
                symbol = str(value.get("symbol", "")).zfill(6)
                assignment = stock_assignment.get(symbol, {})
                primary = assignment.get("primary_theme") if isinstance(assignment, Mapping) else None
                if "main_plate" in value:
                    value["main_plate"] = primary.get("name") if isinstance(primary, Mapping) else None
                for child in value.values():
                    assign_extreme_rows(child)
            elif isinstance(value, list):
                for child in value:
                    assign_extreme_rows(child)
        assign_extreme_rows(payload.get("chapters", {}).get("extremes_board"))
        payload["limit_reason_themes"] = theme_snapshot["assignments"]
        payload["stock_theme_assignment"] = {
            "trade_date": trade_date, "used_date": previous_trade_date,
            "as_of": previous_trade_date,
            "version": next(iter(stock_assignment.values())).get("version") if stock_assignment else None,
            "count": len(stock_assignment), "source_counts": {
                source: sum(row.get("source") == source for row in stock_assignment.values())
                for source in ("POOL_LABEL", "REASON_LATEST", "STATIC", "HOT_PLATE_NAME", "UNKNOWN")},
        }
        payload["theme_attribution_basis"] = theme_snapshot["basis"]
        payload["hot_plate_names"] = theme_snapshot["hot_plate_names"]
        payload["theme_snapshot_status"] = theme_snapshot["status"]
        payload["theme_source_counts"] = theme_snapshot.get("source_counts", {})
        # Keep A→B peers on the same C212 primary theme used by the ladder.
        assigned_counts: dict[str, int] = {}
        themed_stocks = []
        for stock in live_stocks:
            if not isinstance(stock, Mapping):
                continue
            symbol = str(stock.get("symbol", stock.get("code", ""))).zfill(6)
            assignment = theme_snapshot["assignments"].get(symbol, {})
            name = str(assignment.get("theme_name") or "") if isinstance(assignment, Mapping) else ""
            if name and name != "无题材" and assignment.get("theme_source") not in (None, "NONE"):
                assigned_counts[name] = assigned_counts.get(name, 0) + 1
                themed_stocks.append({**stock, "plates": [name], "main_plate": name,
                                      "theme_assignment": assignment})
            else:
                themed_stocks.append({**stock, "plates": [], "theme_assignment": assignment})
        theme_metadata = dict(plate_metadata)
        for name, count in assigned_counts.items():
            theme_metadata.setdefault(name, {"class": "THEME", "size": count, "generic": False})
        if themed_stocks:
            payload["a_to_b"] = build_a_to_b(
                themed_stocks, trade_date=trade_date, stage=stage, asof=point,
                relation_asof=relation_asof, plate_metadata=theme_metadata)
        payload["front_page"] = build_front_page_data(
            payload, stage=stage, plate_metadata=theme_metadata, stocks=themed_stocks,
            theme_assignments=theme_snapshot["assignments"], theme_basis=theme_snapshot["basis"],
            hot_plate_names=theme_snapshot["hot_plate_names"], relation_asof=relation_asof,
            relation_lag_trading_days=relation_lag,
            checkpoint=chosen_checkpoint if stage == "open" else "0925")
        if stage == "auction" and _auction_assembly_mode() == "v6":
            try:
                facts_result = slice_results.get("auction_stock_facts")
                facts_rows = facts_result.rows if facts_result is not None else []
                facts_meta = facts_result.meta if facts_result is not None else {}
                if not facts_rows:
                    raise ValueError("AUCTION_STOCK_FACTS_MISSING")
                facts_quality = facts_meta.get("quality", {}) if isinstance(facts_meta.get("quality"), Mapping) else {}
                facts_asof = facts_meta.get("as_of") or facts_meta.get("asof") or slice_asof
                event_time_replay = facts_meta.get("source") == "core_data_event_time_replay"
                stock_facts = {
                    "trade_date": trade_date, "asof": facts_asof,
                    "version": facts_meta.get("version"), "rows": facts_rows,
                    "summary": facts_quality,
                    "seal_label": facts_quality.get("seal_label") or
                        ("回放·按事件时间" if event_time_replay
                         else "已核实"),
                }
                yesterday_pool_rows = _pool_rows(slice_results["yest_limit_pool"].rows)
                yesterday_reasons = {}
                for reason_row in reason_rows:
                    if not isinstance(reason_row, Mapping):
                        continue
                    reason_type = reason_row.get("Type", reason_row.get("type"))
                    if reason_type not in (None, "", 0, "0"):
                        continue
                    symbol = str(reason_row.get("StockID", reason_row.get("stock_id", reason_row.get("symbol", ""))))
                    if symbol:
                        key = symbol[-6:].zfill(6)
                        # Reason is the concrete limit-up explanation; SCLT is a
                        # streak classification and must not be presented as cause.
                        label = reason_row.get("Reason", reason_row.get("reason"))
                        if label not in (None, ""):
                            event_date = reason_row.get("Date", reason_row.get("date", reason_row.get("event_date")))
                            yesterday_reasons.setdefault(key, []).append({
                                "event_date": str(event_date or "")[:10],
                                "reason": str(label),
                            })
                baseline_rows = slice_results["auction_baseline"].rows
                baseline_replay = trade_date < date.today().isoformat()
                baseline_history = _auction_baseline_history(
                    baseline_rows, trade_date=trade_date, asof=slice_asof,
                    replay_event_time=baseline_replay)
                if baseline_replay and not baseline_history:
                    # Historical previews may read the immutable stored version
                    # whose availability is later than the historical report as-of.
                    from .data_pipeline.storage import ArtifactCatalog
                    baseline_catalog = ArtifactCatalog(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data"))
                    baseline_path = baseline_catalog.locate("auction_baseline", trade_date)
                    if baseline_path is not None and baseline_path.is_file():
                        baseline_history = _auction_baseline_history(
                            baseline_catalog.read_rows(baseline_path), trade_date=trade_date,
                            asof=slice_asof, replay_event_time=True)
                panorama_members = [
                    {"plate_id": str(code), "plate_name": str(plate_names.get(code) or code),
                     "symbol": symbol}
                    for code, members in relations.items() for symbol in members
                ]
                from .auction_panorama import (build_auction_panorama, store_auction_panorama,
                                               normalize_auction_stock_rows, normalize_hot_plates)
                from .data_pipeline.storage import ArtifactCatalog
                hot_catalog = ArtifactCatalog(os.environ.get("CORE_DATA_ROOT", "~/core-data")).locate(
                    "hot_plates", str(stock_facts.get("trade_date")))
                hot_rows, hot_ranks = [], {}
                if hot_catalog and hot_catalog.is_file():
                    hot_meta = json.loads(hot_catalog.read_text(encoding="utf-8"))
                    hot_rows = ArtifactCatalog(os.environ.get("CORE_DATA_ROOT", "~/core-data")).read_rows(hot_catalog)
                    hot_ranks = normalize_hot_plates(hot_rows, fetched_at=hot_meta["fetched_at"],
                        available_at=hot_meta.get("available_at"),
                        asof=stock_facts["asof"], trade_date=stock_facts["trade_date"],
                        now=datetime.now(TZ).isoformat())
                normalized_stock_rows = normalize_auction_stock_rows(stock_facts)
                panorama = build_auction_panorama(
                    stock_facts, plate_members=panorama_members,
                    yesterday_pool=yesterday_pool_rows, yesterday_reasons=yesterday_reasons,
                    theme_assignments=theme_snapshot["assignments"], name_map=stock_names,
                    streak_feedback=payload.get("streak_feedback"),
                    market_amount_history=baseline_history,
                    amount_baseline_basis="REPLAY_EVENT_TIME" if baseline_replay else "PIT",
                    hot_plate_ranks=hot_ranks.get("ranks", {}),
                    plate_classes=class_by_code,
                    previous_trade_date=previous_trade_date,
                    normalized_rows=normalized_stock_rows)
                from .auction_quickbrief import build_auction_quickbrief
                quickbrief = build_auction_quickbrief(
                    stock_facts, plate_members=panorama_members,
                    yesterday_pool=yesterday_pool_rows, plate_classes=class_by_code,
                    name_map=stock_names,
                    market_amount_history=baseline_history,
                    replay=baseline_replay or hot_ranks.get("basis") == "POSTHOC_DEV",
                    normalized_rows=normalized_stock_rows)
                relay_buckets = panorama.get("overview", {}).get("yesterday_reason_buckets", [])
                relay_candidates = [bucket for bucket in relay_buckets
                    if isinstance(bucket, Mapping) and bucket.get("willingness") != "不适用·非共同题材"]
                lead_relay = relay_candidates[0].get("willingness") if relay_candidates else "样本不足"
                highboards = panorama.get("overview", {}).get("yesterday_pool_high_boards", [])
                high_statuses = [item.get("promotion_status") for item in highboards if isinstance(item, Mapping)]
                highboard_state = ("有拟晋级迹象" if "PROVISIONAL" in high_statuses else
                    "暂无可核实高标" if not high_statuses or all(value == "UNVERIFIED" for value in high_statuses) else
                    "未见封板晋级")
                quickbrief.setdefault("rules", {})["panorama_relay_label"] = lead_relay
                quickbrief["rules"]["panorama_highboard_state"] = highboard_state
                quickbrief["email_lines"] = [
                    f"结论:题材接力意愿{lead_relay}；高标{highboard_state}"
                    if str(line).startswith("结论:") else line
                    for line in quickbrief.get("email_lines", [])]
                panorama["quickbrief"] = quickbrief
                payload["auction_quickbrief"] = quickbrief
                panorama["lineage"]["hot_rank_basis"] = hot_ranks.get("basis", "UNKNOWN")
                store_auction_panorama(panorama, redis=src.redis, artifact_root=_dry_run_artifact_root(out_root, dry_run))
                auction_panorama_artifact = panorama
                payload["auction_panorama"] = panorama
            except Exception as exc:
                from .email_render import v6_failure_internal, v6_failure_public_code
                raw_failure = f"{type(exc).__name__}: {exc}"
                payload["v6_panorama_failure"] = v6_failure_public_code(raw_failure)
                v6_failure_internal_detail = v6_failure_internal(raw_failure)
                payload["v6_degraded_code"] = "V6_PANORAMA_MISSING"
                degraded.append("V6_PANORAMA_MISSING")
                failed_facts_result = slice_results.get("auction_stock_facts")
                if failed_facts_result is None or not failed_facts_result.rows:
                    # The optional V6 facts slice is represented by the explicit
                    # panorama failure warning when unavailable, avoiding a false
                    # report-level mandatory-slice failure for legacy rendering.
                    slice_manifest.pop("auction_stock_facts", None)
                    slice_results.pop("auction_stock_facts", None)
                    slice_timings.pop("auction_stock_facts", None)
                    used_dates.pop("auction_stock_facts", None)
        # Ladder rows are rendered before the front-page cluster block; project
        # the shared primary assignment there as well.
        for row in payload.get("ladder_section", {}).get("rows", []):
            assignment = stock_assignment.get(str(row.get("symbol", "")).zfill(6), {})
            primary = assignment.get("primary_theme") if isinstance(assignment, Mapping) else None
            row["main_plate"] = primary.get("name") if isinstance(primary, Mapping) else None
        from .email_render import render_email_html
        pipeline_timings["compute"] = round(time.monotonic() - compute_started, 3)
        render_started = time.monotonic()
        email_page, email_plain = render_email_html(payload, stage, trade_date, {
            "trade_date": trade_date, "asof": asof, "sources": {"td": td_source_manifest},
            "slices": slice_manifest, "used_dates": used_dates, "degraded": degraded,
            "elapsed_seconds": round(time.monotonic() - started, 3)}, full_report=True)
        pipeline_timings["render"] = round(time.monotonic() - render_started, 3)
        write_started = time.monotonic()
        (out / "report.html").write_text(email_page, encoding="utf-8")
        if stage == "auction":
            (out / "report.txt").write_text(email_plain, encoding="utf-8")
        persisted_payload = dict(payload)
        if stage == "auction":
            # The complete immutable panorama is separately stored in Core data;
            # keep the general report JSON below the live-report size ceiling.
            persisted_payload.pop("auction_panorama", None)
        report_json = _shrink_live_report(live_pages._json_report_payload(persisted_payload))
        report_text = json.dumps(report_json, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        pipeline_timings["prepare_output"] = round(time.monotonic() - write_started, 3)
    else:
        close_catalog_rows: list[dict[str, Any]] = []
        close_catalog_manifest: dict[str, Any] = {}
        close_catalog_ready = False
        if stage == "close":
            from .data_pipeline.storage import ArtifactCatalog
            core_root = Path(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data")).expanduser()
            close_catalog_rows, close_catalog_manifest = load_close_catalog_inputs(
                ArtifactCatalog(core_root), trade_date, prior_candidates[-1] if prior_candidates else None,
                asof=(f"{trade_date}T{asof}+08:00" if asof else f"{trade_date}T15:00:00+08:00"),
                basis=close_basis)
            close_catalog_ready = bool(close_catalog_rows)
            for dataset, dataset_info in close_catalog_manifest.get("datasets", {}).items():
                if dataset_info.get("status") == "READY":
                    inputs[f"close_catalog:{dataset}"] = {
                        "manifest": dataset_info.get("manifest"), "version": dataset_info.get("version"),
                        "sha256": dataset_info.get("sha256")}
        capture_manifest = refs["daily_capture_manifest"]
        daily_capture_dir = capture_manifest.parent if capture_manifest else None
        td_daily_error = None
        if stage != "close" and not refs.get("stock_day_facts") and not close_catalog_ready and not src.td_error:
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
        elif not refs.get("stock_day_facts"):
            td_daily_error = src.td_error
        if daily_capture_dir:
            try:
                local_meta = json.loads((daily_capture_dir / "manifest.json").read_text(encoding="utf-8"))
                if str(local_meta.get("date")) != trade_date:
                    daily_capture_dir = None
            except (OSError, ValueError):
                daily_capture_dir = None
        if daily_capture_dir is None and not close_catalog_ready:
            if td_daily_error:
                degraded.append(td_daily_error)
            degraded.append("MISSING_DAILY_KLINE: need TD daily_kline or a local same-date capture")
        elif td_daily_error:
            degraded.append(f"DAILY_KLINE_FALLBACK_LOCAL:{td_daily_error}")
        prev_path = None if stage == "close" else _previous_report(Path(refs.get("prev_reports_root") or root), trade_date,
                                     (STAGE_NAMES["close"], STAGE_NAMES["open"]))
        if close_catalog_ready:
            fact_stocks = close_catalog_rows
            fact_rows = [{"symbol": row["symbol"], "yesterday_limit_up": row["in_yest_pool"],
                          "yesterday_lb_days": row["lb_days"]} for row in fact_stocks]
            amounts = [row["amount_yuan"] for row in fact_stocks if isinstance(row.get("amount_yuan"), (int, float))]
            live_path = None
            data = derive_close_review(fact_stocks, trade_date=trade_date,
                live_report=_read_optional_json(live_path, degraded, "SAME_DAY_REPORT")
                    if live_path and live_path.is_file() else None,
                prev_live=_read_optional_json(prev_path, degraded, "PREVIOUS_REPORT")
                    if prev_path and prev_path.is_file() else None,
                market={"amount_yuan": sum(amounts) if amounts else None,
                        "amount_coverage": len(amounts), "daily_source": "CORE_CATALOG:daily_kline"},
                checkpoint_status={tag: "READY" if any(r.get(f"p{tag}_pct") is not None for r in fact_stocks) else "MISSING"
                                   for tag in ("0932", "0935", "0940", "0945")})
            data["data_quality"]["daily_source"] = "CORE_CATALOG:daily_kline"
            data["data_quality"]["catalog_field_coverage"] = close_catalog_manifest["field_coverage"]
            data["data_quality"]["checkpoints"] = "MISSING; close market statistics remain available"
            close_manifest = close_catalog_manifest
            if close_catalog_manifest.get("field_coverage", {}).get("p0932_pct", {}).get("valid_n", 0) == 0:
                degraded.append("MISSING_CLOSE_CHECKPOINTS:0932-0945")
            markdown = render_close_markdown(data)
            page = render_close_html(data, markdown)
        else:
            data, close_manifest = _close_fallback(trade_date, ["daily_kline"])
            data["reference_data"] = {
                "stock_names": {"status": "READY" if refs["stock_names"] else "MISSING"},
                "stock_caps": {"status": "READY" if refs["stock_caps"] else "MISSING"}}
            degraded.append("MISSING_CLOSE_INPUT:daily_kline")
            markdown = "# 收盘复盘\n\n- Core catalog daily_kline: 缺失\n"
            page = "<!doctype html><meta charset='utf-8'><title>收盘复盘</title><pre>" + markdown + "</pre>"
        if close_catalog_ready:
            streak_counts: dict[int, int] = {}
            for row in fact_rows:
                level = row.get("yesterday_lb_days")
                if row.get("yesterday_limit_up") and isinstance(level, (int, float)) and level > 0:
                    streak_counts[int(level)] = streak_counts.get(int(level), 0) + 1
            ladder = [{"board_height": f"{level}板", "previous_n": count}
                      for level, count in sorted(streak_counts.items())]
            unknown_streaks = sum(bool(row.get("yesterday_limit_up")) and
                                  not isinstance(row.get("yesterday_lb_days"), (int, float))
                                  for row in fact_rows)
            if unknown_streaks:
                data.setdefault("data_gaps", []).append(
                    f"昨日涨停池连板天数缺失：{unknown_streaks}只，未纳入梯队高度计数")
        else:
            ladder = _load_ladder_summary(slice_results["path_stats"].rows, trade_date)
        previous_streaks = {}
        if prev_path and prev_path.is_file() and not close_catalog_ready:
            previous_streaks = _yesterday_lb_days(
                _read_optional_json(prev_path, degraded, "PREVIOUS_REPORT"))
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
                    "p0925_pct": stock.get("p0925_pct"), "p0932_pct": stock.get("p0932_pct"),
                    "p0935_pct": stock.get("p0935_pct"), "p0940_pct": stock.get("p0940_pct"),
                    "p0945_pct": stock.get("p0945_pct"),
                    "close_pct": stock.get("close_pct"), "in_yest_pool": stock.get("in_yest_pool"),
                    "status": "今日最高板；收盘涨停价代理，封单/炸板路径 UNKNOWN"})
            data["ladder_summary"].extend(highboard_rows)
        if isinstance(data.get("stocks"), list) and not close_catalog_ready:
            _attach_range_positions(data["stocks"], slice_results["daily_history"].rows, trade_date,
                                    report_features=slice_results["daily_report_features"].rows or None)
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
        _attach_auction_baseline(data, slice_results["auction_baseline"].rows)
        quality = data.setdefault("data_quality", {})
        quality["basis"] = close_catalog_manifest.get("basis")
        quality["late_sources"] = close_catalog_manifest.get("late_sources", [])
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
        # The wide facts table is the detail store; keep the pipeline JSON under
        # its existing size contract while retaining codes in the compact data.
        gaps = data.get("main_plate_gaps", [])
        gap_total = len(gaps)
        gap_limit = 80
        quality = data.setdefault("data_quality", {})
        quality["main_plate_gaps_total"] = gap_total
        quality["main_plate_gaps_omitted"] = max(0, gap_total - gap_limit)
        if gap_total > gap_limit:
            (out / "main_plate_gaps.json").write_text(
                json.dumps({"trade_date": trade_date, "total": gap_total, "rows": gaps},
                           ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
                encoding="utf-8")
            data["main_plate_gaps"] = gaps[:gap_limit]
        # Final quality metadata must be present in every human and machine view.
        if data.get("status") != "DEGRADED_MISSING_INPUTS":
            markdown = render_close_markdown(data)
            (out / "report.md").write_text(markdown, encoding="utf-8")
            (out / "report.html").write_text(render_close_html(data, markdown), encoding="utf-8")
        report_payload = {key: value for key, value in data.items() if key != "stocks"}
        report_text = json.dumps(report_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        close_manifest["input_files"] = close_manifest.get("input_files", {})
    basis_notes = []
    if any(result.meta.get("legacy_dependency") or result.meta.get("source") == "legacy_redis"
           for result in slice_results.values()):
        basis_notes.append("部分切片来自 legacy_redis 过渡数据")
    if any(result.meta.get("source") == "FILE_FALLBACK" for result in slice_results.values()):
        basis_notes.append("存在 FILE_FALLBACK 回放文件切片")
    if any(result.meta.get("reason") == "NO_ASOF" for result in slice_results.values()):
        basis_notes.append("部分来源无可验证时点(NO_ASOF)")
    report_object = data if stage == "close" else payload
    report_object["slice_data_basis"] = basis_notes or ["仅使用有可用时点信息的 Core 数据集切片"]
    if basis_notes:
        note = "数据口径：" + "；".join(basis_notes) + "。"
        if stage == "close":
            limitations = report_object.get("limitations")
            if not isinstance(limitations, list):
                limitations = [limitations] if limitations else []
            limitations.append(note)
            report_object["limitations"] = limitations
        else:
            old_limitations = str(report_object.get("limitations") or "")
            report_object["limitations"] = (old_limitations + " " + note).strip()
        if stage == "close":
            markdown += "\n\n" + note + "\n"
            (out / "report.md").write_text(markdown, encoding="utf-8")
        import html
        note_html = "<p>" + html.escape(note) + "</p>"
        html_path = out / "report.html"
        # Append to the report actually written for this stage (the email-styled page for auction/open),
        # never overwrite it with the earlier render_page output.
        written = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
        head, sep, tail = written.rpartition("</body>")
        written = (head + note_html + sep + tail) if sep else (written + note_html)
        html_path.write_text(written, encoding="utf-8")
    compact_report = ({key: value for key, value in data.items() if key != "stocks"}
                      if stage == "close" else
                      live_pages._json_report_payload(persisted_payload if stage == "auction" else payload))
    _shrink_live_report(compact_report)
    if stage == "auction":
        compact_report["trade_date"] = trade_date
        if isinstance(auction_panorama_artifact, Mapping):
            compact_report["asof"] = auction_panorama_artifact.get("asof")
            panorama_bytes = json.dumps(auction_panorama_artifact, ensure_ascii=False,
                                        allow_nan=False, separators=(",", ":")).encode("utf-8")
            panorama_hash = hashlib.sha256(panorama_bytes).hexdigest()
            panorama_sidecar = {"trade_date": trade_date, "asof": auction_panorama_artifact.get("asof"),
                "version": auction_panorama_artifact.get("version") or auction_panorama_artifact.get("schema"),
                "sha256": panorama_hash, "panorama": auction_panorama_artifact}
            sidecar_bytes = json.dumps(panorama_sidecar, ensure_ascii=False, allow_nan=False,
                                       separators=(",", ":")).encode("utf-8")
            (out / "auction_panorama.json").write_bytes(sidecar_bytes)
            compact_report["auction_panorama_file"] = "auction_panorama.json"
            compact_report["auction_panorama_sha256"] = hashlib.sha256(sidecar_bytes).hexdigest()
        elif asof:
            compact_report["asof"] = asof
    elif stage == "open" and asof:
        compact_report["asof"] = f"{trade_date}T{asof}"
        compact_report["checkpoint"] = chosen_checkpoint
        stamp = f"报告时点：{asof[:5]}；数据截点：{chosen_checkpoint or '无'}"
        import html
        rendered = (out / "report.html").read_text(encoding="utf-8")
        notice = f'<p class="report-asof">{html.escape(stamp)}</p>'
        (out / "report.html").write_text(rendered.replace("</body>", notice + "</body>", 1),
                                         encoding="utf-8")
    elif stage == "open" and chosen_checkpoint:
        from .live_session import OPEN_CHECKPOINTS
        cutoff = dict(OPEN_CHECKPOINTS)[chosen_checkpoint]
        compact_report["asof"] = f"{trade_date}T{cutoff}"
        compact_report["checkpoint"] = chosen_checkpoint
    if stage == "open" and chosen_checkpoint:
        from .live_session import (ReportCheckpointInvariantError,
                                   assert_report_checkpoint_invariant)
        checkpoint_violation_count = 0
        while True:
            try:
                assert_report_checkpoint_invariant(compact_report, chosen_checkpoint)
                break
            except ReportCheckpointInvariantError as exc:
                checkpoint_violation_count += 1
                degraded.append(exc.code)
                compact_report = _degraded_checkpoint_report(
                    compact_report, trade_date, chosen_checkpoint, asof)
                banner = ('<!doctype html><html><body><div class="report-degraded">'
                          '报告包含时点校验失败字段，相关数据已隐藏。</div></body></html>')
                html_path = out / "report.html"
                html_path.write_text(banner, encoding="utf-8")
    report_text = json.dumps(compact_report,
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    report_json_warning = None
    if len(report_text.encode("utf-8")) > 250_000:
        try:
            _obj = json.loads(report_text)
            _sizes = sorted(((len(json.dumps(v, ensure_ascii=False)), k) for k, v in _obj.items()), reverse=True)[:5]
        except Exception:
            _sizes = []
        report_json_warning = "report.json exceeds 250 KB; largest keys: " + ", ".join(
            f"{k}={n}" for n, k in _sizes)
    write_started = time.monotonic()
    (out / "report.json").write_text(report_text, encoding="utf-8")
    pipeline_timings["write_json"] = round(time.monotonic() - write_started, 3)
    if stage != "close":
        close_manifest = {}
    manifest = {"contract": "DailyPipelineManifestV1", "trade_date": trade_date,
                "stage": stage, "asof": asof, "checkpoint": chosen_checkpoint,
                "outputs": str(out), "input_files": inputs,
                "auction_assembly": (_auction_assembly_mode() if stage == "auction" else None),
                "sources": {"td": td_source_manifest,
                            "q2_redis": {"available": src.redis is not None and not degraded_q2,
                                         "coverage": q2_coverage if src.redis is not None and not degraded_q2 else None},
                            "kaipan_relation": {"path": str(relation_dir) if relation_dir else None,
                                                "asof": relation_asof,
                                                "lag_trading_days": relation_lag if relation_lag is not None else "UNKNOWN",
                                                "coverage": len(relations)},
                            "daily_kline": {"available": refs["daily_capture_manifest"] is not None,
                                            "path": str(refs["daily_capture_manifest"]) if refs["daily_capture_manifest"] else None,
                                            "coverage": daily_coverage}},
                "degraded": sorted(set(degraded)), "close_review_manifest": close_manifest,
                "degraded_counts": ({"REPORT_CHECKPOINT_INVARIANT_VIOLATION": checkpoint_violation_count}
                                    if stage == "open" and checkpoint_violation_count else {}),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "timings": pipeline_timings,
                "slices": {name: {**slice_manifest[name], "elapsed_seconds": slice_timings[name]}
                           for name in slice_results},
                "used_date": used_dates,
                "used_dates": used_dates,
                "data_provenance_flags": sorted({
                    "FILE_FALLBACK" if result.meta.get("source") == "FILE_FALLBACK" else
                    "NO_ASOF" if result.meta.get("reason") == "NO_ASOF" else
                    "legacy_redis" if result.meta.get("legacy_dependency") or
                    result.meta.get("source") == "legacy_redis" else ""
                    for result in slice_results.values()} - {""}),
                "report_json_bytes": len(report_text.encode("utf-8")), "allow_wencai_fallback": allow_wencai_fallback}
    if v6_failure_internal_detail:
        manifest["v6_panorama_failure_internal"] = v6_failure_internal_detail
    if report_json_warning:
        manifest["warnings"] = [report_json_warning]
    print("REPORT_TIMINGS " + " ".join(f"{key}={value:.3f}s" for key, value in pipeline_timings.items()),
          file=sys.stderr)
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
                                       attach_full_html=False)
            receipt = send_message(mail_config, mail, dry_run=not email_send,
                                  allow_recipients=email_to)
            manifest["email"] = {"status": receipt.status}
        except Exception as exc:
            manifest["email"] = {"status": "failed", "error_class": type(exc).__name__}
    try:
        from .report_index import append_record, build_record
        report_catalog_root = _dry_run_artifact_root(out_root, dry_run) or catalog_root
        completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        index_record = build_record(manifest, out_dir=out, completed_at=completed_at)
        indexed = append_record(report_catalog_root, index_record)
        manifest["report_index"] = {"version": indexed["version"],
                                    "revision": indexed["revision"]}
    except Exception as exc:
        manifest["degraded"].append("REPORT_INDEX_WRITE_FAILED")
        manifest["report_index_error_class"] = type(exc).__name__
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
    return parser


def main(argv: list[str] | None = None, *, sources: DailySources | None = None) -> int:
    args = build_parser().parse_args(argv)
    reference_root = os.getenv("CORE_REFERENCE_ROOT")
    if args.reference_catalog and os.getenv("CORE_ALLOW_FILE_FALLBACK") != "1":
        print("WARNING --reference-catalog is deprecated; set CORE_ALLOW_FILE_FALLBACK=1 for explicit replay only",
              file=sys.stderr)
    result = run_daily_pipeline(stage=args.stage, trade_date=args.trade_date, asof=args.asof,
                                out_root=args.out_root, reference_root=reference_root,
                                sources=sources, publish=args.publish, dry_run=args.dry_run,
                                allow_wencai_fallback=args.allow_wencai_fallback,
                                reference_catalog=args.reference_catalog,
                                email_to=args.email_to, email_send=args.email_send,
                                close_basis=("POSTHOC_REVIEW" if args.stage == "close" and
                                             os.getenv("POSTHOC_DEV") == "1" and args.dry_run else "LIVE_PIT"))
    print(f"trade_date={args.trade_date} stage={args.stage} out={result['out_dir']} "
          f"degraded={len(result['manifest']['degraded'])} report_json_bytes={result['manifest']['report_json_bytes']}")
    return 0
