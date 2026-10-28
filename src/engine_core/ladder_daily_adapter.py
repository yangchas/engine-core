"""Thin adapter for ``ladder_daily`` (C577): load inputs, run the pure builder, commit one version.

The adapter owns everything the pure builder must not touch: the catalog, the trading calendar, the (throttled)
live Kaipan / WenCai rungs and the commit. The builder asks for inputs it does not have (``NeedInput``); the adapter
supplies them lazily, so a typical day loads only the 3-8 trading days a live streak needs.

Rungs are configuration driven (registry ``ladder_daily.derive``); a rung that is not configured is never tried.
"""
from __future__ import annotations

import importlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .data_pipeline.derived_daily import UpstreamNotReady, inputs_stale, normalize_status, select_best
from .data_pipeline.ladder_daily import ALGORITHM, DEFAULT_LADDER, NeedInput, build_ladder_daily
from .data_pipeline.storage import ArtifactCatalog

DEFAULT_CONFIG = {"source_ladder": list(DEFAULT_LADDER), "window_days": 20, "continuity_min_ratio": 0.95,
                  "kaipan": {"live": True, "max_requests": 12},
                  "wencai": {"live": True, "min_interval_seconds": 10.0, "max_queries_per_run": 2,
                             "cookie_path": "~/core-data/secrets/wencai.cookie"}}


def trading_days(root: Path, upto: str) -> list[str]:
    try:
        matrix = json.loads((Path(root) / "trade_calendar" / "calendar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpstreamNotReady("TRADE_CALENDAR_MISSING", str(root)) from exc
    return sorted(str(r["d"]) for r in matrix.get("days", []) if r.get("t") == 1 and str(r["d"]) <= upto)


def _best_manifest(catalog: Mapping[str, Any], dataset: str, day: str):
    entries = catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) or []
    if isinstance(entries, Mapping):
        entries = [entries]
    best = select_best([e for e in entries if isinstance(e, Mapping)])
    if best is None or not best.get("manifest"):
        return None, None
    path = Path(str(best["manifest"]))
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None


class LazyKaipanClient:
    def get_his_bans(self, day: str, level: int) -> Any:
        sdk = importlib.import_module("pykaipan.pykaipan")
        return sdk.getHisBans(date=day, ban=str(level), size="2000")


def default_kaipan_fetch(max_requests: int = 12) -> Callable[[str], list[dict]]:
    def fetch(day: str) -> list[dict]:
        from .data_pipeline.providers.kaipan_provider import KaipanProvider
        return list(KaipanProvider(LazyKaipanClient(), max_requests=max_requests).limit_up_pool(day)["rows"])
    return fetch


def default_wencai_fetch(cfg: Mapping[str, Any], state_path: Path) -> Callable[[str], dict]:
    """WenCai "YYYY-MM-DD涨停" with a persistent min-interval gate and a per-run query budget; the day is only
    accepted when the dated column headers ``[YYYYMMDD]`` carry the requested day (date evidence)."""
    used = {"n": 0}

    def fetch(day: str) -> dict:
        if used["n"] >= int(cfg.get("max_queries_per_run", 2)):
            return {"status": "unavailable", "reason": "wencai_query_budget_exhausted"}
        interval = float(cfg.get("min_interval_seconds", 10.0))
        try:
            last = float(json.loads(state_path.read_text(encoding="utf-8")).get("last_request", 0))
        except (OSError, ValueError):
            last = 0.0
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_request": time.time()}), encoding="utf-8")
        used["n"] += 1
        cookie = None
        cookie_path = Path(str(cfg.get("cookie_path") or "")).expanduser()
        if cfg.get("cookie_path") and cookie_path.is_file():
            cookie = cookie_path.read_text(encoding="utf-8").strip() or None   # never printed or logged
        from pywencai.wencai import get_robot_data
        kwargs = {"query": f"{day}涨停", "query_type": "stock", "retry": 0, "strict": True}
        if cookie:
            kwargs["cookie"] = cookie
        result = get_robot_data(**kwargs)
        rows = []
        if hasattr(result, "to_dict"):
            rows = [dict(r) for r in result.to_dict(orient="records")]
        elif isinstance(result, list):
            rows = [dict(r) for r in result if isinstance(r, Mapping)]
        stamps = {m.group(1) for r in rows for h in r for m in [re.search(r"\[(\d{8})\]$", str(h))] if m}
        evidence = None
        if len(stamps) == 1:
            s = stamps.pop()
            evidence = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        out = []
        for r in rows:
            code = next((str(v) for k, v in r.items() if str(k).startswith("股票代码") or str(k) == "code"), "")
            if code[:6].isdigit():
                out.append({"symbol": code[:6]})
        return {"status": "ok" if out else "unavailable", "rows": out, "date_evidence": evidence,
                "reason": "" if out else "wencai_empty"}
    return fetch


class Loader:
    """Supplies ``days[date][rung]`` on demand from the catalog (and the configured live rungs)."""

    def __init__(self, catalog: ArtifactCatalog, cfg: Mapping[str, Any], *, kaipan_fetch=None, wencai_fetch=None,
                 when: datetime | None = None):
        self.catalog, self.cfg = catalog, cfg
        self.when = when or datetime.now(timezone.utc)
        self.snapshot = catalog.read()
        self.kaipan_fetch, self.wencai_fetch = kaipan_fetch, wencai_fetch
        self.days: dict[str, dict[str, Any]] = {}
        self.live_log: list[dict[str, Any]] = []

    def load(self, day: str, rung: str) -> None:
        slot = self.days.setdefault(day, {})
        if rung == "daily_kline":
            path, manifest = _best_manifest(self.snapshot, "daily_kline", day)
            slot[rung] = ({"status": "unavailable", "reason": "no_partition"} if manifest is None else
                          {"status": "ok", "version": manifest.get("version"),
                           "business_status": manifest.get("business_status"), "source": manifest.get("source"),
                           "available_at": manifest.get("available_at"), "rows": self.catalog.read_rows(path)})
        elif rung == "kaipan":
            path, manifest = _best_manifest(self.snapshot, "limit_up_pool", day)
            if manifest is not None:
                slot[rung] = {"status": "ok", "version": manifest.get("version"),
                              "business_status": manifest.get("business_status"), "source": manifest.get("source"),
                              "available_at": manifest.get("available_at"), "rows": self.catalog.read_rows(path)}
            else:
                slot[rung] = self._live(day, "kaipan", self.kaipan_fetch, self.cfg.get("kaipan"))
        elif rung == "wencai":
            slot[rung] = self._live(day, "wencai", self.wencai_fetch, self.cfg.get("wencai"))
        else:
            raise ValueError("unknown_rung:" + rung)

    def preload_kaipan_cross(self, day: str) -> None:
        """Catalog-only (no network): load the Kaipan pool next to a daily-bar day for the cross validation."""
        slot = self.days.setdefault(day, {})
        if "kaipan" in slot:
            return
        path, manifest = _best_manifest(self.snapshot, "limit_up_pool", day)
        if manifest is not None:
            self.load(day, "kaipan")

    def _live(self, day: str, rung: str, fetch, cfg) -> dict[str, Any]:
        if fetch is None or not (cfg or {}).get("live", False):
            return {"status": "unavailable", "reason": f"{rung}_live_not_configured"}
        started = time.monotonic()
        try:
            got = fetch(day)
        except Exception as exc:  # a failing rung is a missing rung, recorded; never a made-up set
            self.live_log.append({"day": day, "rung": rung, "error": type(exc).__name__ + ":" + str(exc)[:120]})
            return {"status": "unavailable", "reason": f"{rung}_error:{type(exc).__name__}"}
        if isinstance(got, Mapping):
            entry = dict(got)
        else:
            entry = {"status": "ok" if got else "unavailable", "rows": list(got or []), "reason": "empty"}
        rows = [dict(r, trade_date=day) if rung == "kaipan" else r for r in entry.get("rows", [])]
        entry.update(rows=rows, source=rung + "_live", available_at=self.when.isoformat())   # a live fetch is available at the run's own instant
        self.live_log.append({"day": day, "rung": rung, "rows": len(rows), "status": entry.get("status"),
                              "elapsed_ms": round((time.monotonic() - started) * 1000)})
        return entry


def up_to_date(catalog: ArtifactCatalog, trade_date: str) -> bool:
    snapshot = catalog.read()
    entries = snapshot.get("datasets", {}).get("ladder_daily", {}).get("dates", {}).get(trade_date) or []
    best = select_best([e for e in entries if isinstance(e, Mapping)])
    if best is None:
        return False
    _path, manifest = _best_manifest(snapshot, "ladder_daily", trade_date)
    if not manifest or manifest.get("algorithm_version") != ALGORITHM:
        return False
    return not inputs_stale(snapshot, "ladder_daily", trade_date, manifest)


def materialize_ladder_daily(catalog: ArtifactCatalog, *, trade_date: str, config: Mapping[str, Any] | None = None,
                             now: datetime | None = None, force: bool = False, kaipan_fetch=None, wencai_fetch=None,
                             state_dir: Path | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not force and up_to_date(catalog, trade_date):
        return {"status": "UP_TO_DATE", "dataset": "ladder_daily", "trade_date": trade_date}
    started = time.monotonic()
    when = now or datetime.now(timezone.utc)
    calendar = trading_days(catalog.root, trade_date)
    if trade_date not in calendar:
        raise UpstreamNotReady("TRADE_DAY_NOT_IN_CALENDAR", trade_date)
    ladder = list(cfg["source_ladder"])
    if kaipan_fetch is None and "kaipan" in ladder and (cfg.get("kaipan") or {}).get("live"):
        kaipan_fetch = default_kaipan_fetch(int(cfg["kaipan"].get("max_requests", 12)))
    if wencai_fetch is None and "wencai" in ladder and (cfg.get("wencai") or {}).get("live"):
        base = Path(state_dir) if state_dir else catalog.root / "guardian-native"
        wencai_fetch = default_wencai_fetch(cfg["wencai"], base / "ladder_wencai_gate.json")
    loader = Loader(catalog, cfg, kaipan_fetch=kaipan_fetch, wencai_fetch=wencai_fetch, when=when)
    listings = _listings(catalog)
    request = {"trade_date": trade_date, "as_of": when.isoformat(), "fetched_at": when.isoformat(),
               "calendar": calendar, "source_ladder": ladder,
               "window": {"max_days": int(cfg["window_days"])},
               "continuity_min_ratio": float(cfg["continuity_min_ratio"]), "days": loader.days,
               "listings": listings}
    for _ in range(4 * int(cfg["window_days"]) * (len(ladder) + 2)):
        try:
            for day in list(loader.days):  # cross validation input for every loaded daily-bar day (catalog only)
                if "daily_kline" in loader.days[day]:
                    loader.preload_kaipan_cross(day)
            partition = build_ladder_daily(request)
            break
        except NeedInput as need:
            loader.load(need.date, need.rung)
    else:
        raise RuntimeError("ladder_input_loop_did_not_converge")
    manifest = dict(partition.manifest)
    if loader.live_log:
        manifest["live_fetches"] = loader.live_log
    extra = {"clock": (lambda: now)} if now is not None else {}
    committed = catalog.commit("ladder_daily", trade_date, partition.rows, manifest, source=manifest["provider"],
                               **extra)
    return {"status": "COMMITTED", "dataset": "ladder_daily", "trade_date": trade_date,
            "version": committed["version"], "business_status": committed["business_status"],
            "row_count": committed["row_count"], "provider": committed["provider"],
            "provider_chain": committed["provider_chain"], "max_streak": committed["quality"]["max_streak"],
            "live_fetches": loader.live_log}


def _listings(catalog: ArtifactCatalog) -> dict[str, str]:
    path, manifest = _best_manifest(catalog.read(), "stock_static", _latest_day(catalog, "stock_static"))
    if manifest is None:
        return {}
    return {str(r["symbol"]).zfill(6): str(r["listing_date"])[:10]
            for r in catalog.read_rows(path) if r.get("symbol") and r.get("listing_date")}


def _latest_day(catalog: ArtifactCatalog, dataset: str) -> str:
    days = sorted(catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {}))
    return days[-1] if days else ""
