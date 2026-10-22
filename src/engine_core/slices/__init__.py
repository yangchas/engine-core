"""Small, read-only data slice API used by report entry points."""
from __future__ import annotations

import gzip
import json
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo
from engine_core.data_pipeline.storage import available_at_trust

STATUSES = {"READY", "PARTIAL", "MISSING", "STALE", "DEGRADED"}


@dataclass(frozen=True)
class SliceResult:
    rows: list[dict[str, Any]]
    meta: dict[str, Any]

    @classmethod
    def create(cls, rows: list[dict[str, Any]], meta: Mapping[str, Any]) -> "SliceResult":
        if meta.get("status") not in STATUSES:
            raise ValueError("invalid_slice_status")
        return cls(rows, dict(meta))


@dataclass(frozen=True)
class SliceRequest:
    slice_id: str
    trade_date: str
    asof: str
    params: Mapping[str, Any]


Provider = Callable[["SliceReader", SliceRequest], SliceResult]


class SliceReader:
    """Per-report reader with provider priority and request-level singleflight."""

    def __init__(self, providers: Mapping[str, list[Provider] | tuple[Provider, ...]] | None = None,
                 *, snapshot_id: str = "report"):
        self.providers = {key: tuple(value) for key, value in (providers or {}).items()}
        self.snapshot_id = snapshot_id
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], Future[SliceResult]] = {}
        self._manifest_cache: dict[tuple[Any, ...], Future[dict[str, Any]]] = {}
        self._row_cache: dict[tuple[Any, ...], Future[list[dict[str, Any]]]] = {}
        self._as_rows_cache: dict[tuple[Any, ...], Future[list[dict[str, Any]]]] = {}
        self._pit_cache: dict[tuple[Any, ...], Future[tuple[list[dict[str, Any]], bool]]] = {}

    def read_manifest(self, path: str | Path) -> dict[str, Any]:
        key = str(Path(path).resolve())
        with self._lock:
            future = self._manifest_cache.get((key,))
            owner = future is None
            if owner:
                future = Future()
                self._manifest_cache[(key,)] = future
        if owner:
            try:
                future.set_result(json.loads(Path(path).read_text(encoding="utf-8")))
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def read_rows(self, catalog: Any, path: str | Path) -> list[dict[str, Any]]:
        """Read and convert an immutable artifact once per report run."""
        key = str(Path(path).resolve())
        with self._lock:
            future = self._row_cache.get((key,))
            owner = future is None
            if owner:
                future = Future()
                self._row_cache[(key,)] = future
        if owner:
            try:
                future.set_result(catalog.read_rows(path))
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def as_rows(self, rows: Any) -> list[dict[str, Any]]:
        key = (id(rows),)
        with self._lock:
            future = self._as_rows_cache.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._as_rows_cache[key] = future
        if owner:
            try:
                future.set_result(_as_rows(rows))
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def pit_rows(self, rows: list[dict[str, Any]], asof: str) -> tuple[list[dict[str, Any]], bool]:
        """Apply the same PIT cutoff once to the same loaded artifact rows."""
        key = (id(rows), asof)
        with self._lock:
            future = self._pit_cache.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._pit_cache[key] = future
        if owner:
            try:
                future.set_result(_pit_rows(rows, asof))
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def get(self, slice_id: str, *, trade_date: str, asof: str | None = None,
            **params: Any) -> SliceResult:
        if not asof:
            raise ValueError("slice_asof_required")
        cutoff = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("slice_asof_timezone_required")
        key = (slice_id, trade_date, cutoff.isoformat(), json.dumps(params, sort_keys=True, default=str))
        with self._lock:
            future = self._cache.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._cache[key] = future
        if owner:
            try:
                request = SliceRequest(slice_id, trade_date, cutoff.isoformat(), dict(params))
                result = self._resolve(request)
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def _resolve(self, request: SliceRequest) -> SliceResult:
        for provider in self.providers.get(request.slice_id, ()):
            result = provider(self, request)
            if result.meta.get("slice_id") != request.slice_id:
                raise ValueError("slice_provider_identity_mismatch")
            # Preserve provider-attempt lineage for an exhausted chain. A plain
            # missing result still falls through, while an attempted chain
            # must remain visible even when every source returned no rows.
            if result.meta.get("status") != "MISSING" or result.meta.get("lineage"):
                source_rows = result.rows
                if request.slice_id in {"hot_plates_today", "hot_plates_yesterday"}:
                    source_rows = _canonical_hot_plate_rows(source_rows)
                pit_filtered = bool(result.meta.get("_pit_filtered"))
                rows, unknown = ((source_rows, result.meta.get("reason") == "NO_ASOF")
                                 if pit_filtered else self.pit_rows(source_rows, request.asof))
                if request.slice_id in {"hot_plates_today", "hot_plates_yesterday"}:
                    rows = _latest_hot_plate_rows(rows, request.asof)
                available_at = result.meta.get("available_at")
                if available_at:
                    parsed, cutoff = _parse_pit_timestamp(available_at), _parse_pit_timestamp(request.asof)
                    if parsed is None or cutoff is None or parsed > cutoff:
                        continue
                meta = dict(result.meta)
                meta.pop("_pit_filtered", None)
                if len(rows) != len(result.rows):
                    meta["quality"] = {**meta.get("quality", {}), "partial": True,
                                       "missing": "FUTURE_ROWS_FILTERED"}
                    meta["status"] = "PARTIAL"
                    meta["reason"] = "FUTURE_ROWS_FILTERED"
                if unknown:
                    meta["quality"] = {**meta.get("quality", {}), "partial": True,
                                       "missing": "NO_ASOF"}
                    if meta["status"] == "READY":
                        meta["status"] = "PARTIAL"
                        meta["reason"] = "NO_ASOF"
                meta["snapshot_id"] = self.snapshot_id
                return SliceResult.create(rows, meta)
        return _result(request, [], source="none", status="MISSING", reason="NO_PROVIDER_DATA")


def _result(request: SliceRequest, rows: list[dict[str, Any]], *, source: str,
            status: str, reason: str | None = None, version: str | None = None,
            available_at: str | None = None, quality: Mapping[str, Any] | None = None,
            lineage: list[dict[str, Any]] | None = None,
            pit_date: str | None = None, pit_filtered: bool = False) -> SliceResult:
    meta = {"slice_id": request.slice_id, "trade_date": request.trade_date,
            "asof": request.asof, "source": source, "version": version,
            "built_at": datetime.now().astimezone().isoformat(), "available_at": available_at,
            "quality": dict(quality or {"coverage": None, "missing": reason, "partial": False}),
            "status": status, "lineage": lineage or [], "snapshot_id": "report"}
    if pit_date:
        meta["pit_date"] = pit_date
    if reason:
        meta["reason"] = reason
    if pit_filtered:
        meta["_pit_filtered"] = True
    return SliceResult.create(rows, meta)


class SliceProviders:
    """Core artifact -> read-only legacy Redis -> explicit opt-in file adapters."""

    DATASETS = {
        "stock_name_map": "stock_static", "plate_registry": "plate_registry",
        "plate_members": "plate_members", "daily_history": "daily_history_compact",
        "auction_baseline": "auction_baseline", "yest_limit_pool": "limit_up_pool",
        "plate_amount_baseline": "plate_amount_baseline",
        "limit_reason_history": "limit_reasons", "hot_plates_today": "hot_plates",
        "hot_plates_yesterday": "hot_plates", "hot_rank": "hot_rank",
        "hot_plate_minutes": "hot_plate_minutes",
        "auction_points": "auction_points",
        "auction_stock_facts": "auction_stock_facts",
        "auction_panorama": "auction_panorama",
        "trading_calendar": "trade_calendar", "path_stats": "path_stats",
        "daily_report_features": "daily_report_features",
        "stock_theme_assignment": "stock_theme_assignment",
    }

    def __init__(self, *, catalog: Any = None, redis: Any = None, artifact_root: str | Path | None = None,
                 file_fallback: bool | None = None,
                 auction_td_provider: Callable[[SliceRequest], Mapping[str, Any] | None] | None = None,
                 limit_input_provider: Callable[[SliceRequest], Mapping[str, Any] | None] | None = None,
                 wencai_market_gate: Any = None, coverage_threshold: float = 0.95):
        from engine_core.data_pipeline.storage import ArtifactCatalog
        root = Path(artifact_root or os.getenv("CORE_DATA_ROOT", str(Path.home() / "core-data")))
        self.catalog = catalog or ArtifactCatalog(root)
        self.redis = redis
        self.file_fallback = ((os.getenv("CORE_ALLOW_FILE_FALLBACK") == "1")
                              if file_fallback is None else file_fallback)
        from .market_fallbacks import MarketFallbackProviders
        self.market = MarketFallbackProviders(
            auction_td=auction_td_provider, limit_inputs=limit_input_provider,
            redis=redis, wencai_gate=wencai_market_gate,
            coverage_threshold=coverage_threshold,
        )

    def providers(self) -> dict[str, list[Provider]]:
        names = list(self.DATASETS)
        result = {name: [self._core, self._legacy, self._file] for name in names}
        result.update(self.market.providers())
        return result

    def _core(self, reader: SliceReader, request: SliceRequest) -> SliceResult:
        dataset = self.DATASETS[request.slice_id]
        date = request.trade_date
        if request.slice_id == "auction_stock_facts":
            # Hot Top50/seal projections are lookup aids, never a complete
            # market universe for panorama aggregation.
            manifest_path = self._select_manifest(dataset, date, request.asof, reader=reader, exact_date=True)
            if manifest_path is None or not manifest_path.is_file():
                return _result(request, [], source="core_data", status="MISSING",
                               reason="AUCTION_STOCK_FACTS_NOT_IMPORTED",
                               quality={"coverage": 0, "partial": True})
            try:
                manifest = reader.read_manifest(manifest_path)
                if manifest.get("trade_date") != date:
                    return _result(request, [], source="core_data", status="MISSING",
                                   reason="AUCTION_STOCK_FACTS_TRADE_DATE_MISMATCH",
                                   quality={"coverage": 0, "partial": True})
                rows = reader.read_rows(self.catalog, manifest_path)
            except (OSError, ValueError, TypeError, KeyError):
                manifest, rows = {}, []
            if not rows:
                return _result(request, [], source="core_data", status="MISSING",
                               reason="AUCTION_STOCK_FACTS_EMPTY",
                               quality={**manifest.get("quality", {}), "coverage": 0, "partial": True},
                               version=str(manifest.get("version") or "unknown"),
                               available_at=manifest.get("available_at"), pit_date=manifest.get("trade_date"))
            return _result(request, rows, source="core_data", status="PARTIAL",
                           version=str(manifest.get("version") or "unknown"),
                           available_at=manifest.get("available_at"), pit_date=date,
                           quality={**manifest.get("quality", {}), "partial": True},
                           lineage=[{"dataset": dataset, "source": "core_data",
                                     "requested_date": date, "used_date": manifest.get("trade_date"),
                                     "available_at_trust": available_at_trust(manifest)}])
        if request.slice_id == "auction_panorama":
            cutoff = _parse_pit_timestamp(request.asof)
            key = f"core:cache:data:auction_panorama:{date}"
            hot = None
            try:
                raw = self.redis.get(key) if self.redis is not None else None
                hot = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
                available = _parse_pit_timestamp((hot or {}).get("available_at"))
                if not (isinstance(hot, dict) and hot.get("trade_date") == date
                        and hot.get("schema") == "auction_panorama_v1" and hot.get("version")
                        and available is not None and cutoff is not None and available <= cutoff):
                    hot = None
            except (OSError, ValueError, TypeError):
                hot = None
            manifest_path = self._select_manifest(dataset, date, request.asof, reader=reader)
            if manifest_path is None or not manifest_path.is_file():
                return _result(request, [], source="core_data", status="MISSING",
                               reason="AUCTION_PANORAMA_NOT_IMPORTED", quality={"coverage": 0})
            try:
                manifest = reader.read_manifest(manifest_path)
                product_rows = reader.read_rows(self.catalog, manifest_path)
                rows = []
                for row in product_rows:
                    payload = row.get("product_json")
                    product = json.loads(payload) if payload else row.get("product")
                    if isinstance(product, dict):
                        rows.append(product)
            except (OSError, ValueError, TypeError, KeyError):
                manifest, rows = {}, []
            return _result(request, rows, source="core_data", status="PARTIAL",
                           version=str(manifest.get("version") or "unknown"),
                           available_at=manifest.get("available_at"), pit_date=date,
                           quality={**manifest.get("quality", {}), "partial": True,
                                    "hot_view": "INDEX" if hot else "COLD_SLICE"},
                           reason=("DATE_FALLBACK" if manifest.get("trade_date", date) != request.trade_date else None),
                           lineage=[{"dataset": dataset, "source": "core_data",
                                     "requested_date": request.trade_date,
                                     "used_date": manifest.get("trade_date", date),
                                     "available_at_trust": available_at_trust(manifest),
                                     "hot_index": bool(hot)}])
        if request.slice_id == "stock_theme_assignment":
            # Prefer the prepared hot view; a stale or malformed view falls
            # through to the immutable cold artifact below.
            previous = str(request.params.get("prior_trade_date") or "")
            cutoff = _parse_pit_timestamp(request.asof)
            key = f"core:cache:data:stock_theme_assignment:{request.trade_date}"
            try:
                raw = self.redis.get(key) if self.redis is not None else None
                payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
                available = _parse_pit_timestamp((payload or {}).get("as_of"))
                assignments = (payload or {}).get("assignments", {})
                rows = list(assignments.values()) if isinstance(assignments, dict) else []
                if (isinstance(payload, dict) and payload.get("trade_date") == request.trade_date
                        and payload.get("used_date") == previous and payload.get("input_version")
                        and payload.get("visibility_sha256") and rows
                        and available is not None and cutoff is not None and available <= cutoff
                        and all(row.get("available_at") == payload.get("as_of")
                                and row.get("input_version") == payload.get("input_version")
                                and row.get("visibility_sha256") == payload.get("visibility_sha256")
                                for row in rows)):
                    for row in rows:
                        row.setdefault("trade_date", request.trade_date)
                        row.setdefault("used_date", payload["used_date"])
                        row.setdefault("as_of", payload["as_of"])
                    return _result(request, rows, source="core_redis", status="READY",
                                   version=str(payload.get("version") or payload["input_version"]),
                                   available_at=payload.get("as_of"), pit_date=previous,
                                   quality={"coverage": len(rows), "partial": False},
                                   lineage=[{"dataset": dataset, "hot_view": True,
                                             "used_date": previous}])
            except Exception:
                pass
        if request.slice_id == "hot_plates_today":
            # Today's hot plates are selected captures from today's session.
            # Keep the D partition and let manifest/row availability enforce
            # the report as-of cutoff; D-1 is a separate comparison slice.
            date = request.trade_date
        if request.slice_id == "daily_report_features":
            feature_date = str(request.params.get("prior_trade_date") or "")
            if not feature_date:
                return _result(request, [], source="core_data", status="PARTIAL",
                               reason="PRIOR_TRADE_DATE_MISSING",
                               quality={"coverage": 0, "partial": True, "fallback": "daily_history"})
            cutoff = _parse_pit_timestamp(request.asof)
            key = f"core:cache:data:daily_report_features:{feature_date}"
            try:
                raw = self.redis.get(key) if self.redis is not None else None
                payload = json.loads(gzip.decompress(raw)) if raw else None
                available = _parse_pit_timestamp((payload or {}).get("available_at"))
                if (isinstance(payload, dict) and payload.get("dataset") == "daily_report_features"
                        and payload.get("trade_date") == feature_date
                        and payload.get("source") == "core_data"
                        and payload.get("row_count") == len(payload.get("rows", []))
                        and available is not None and cutoff is not None and available <= cutoff):
                    return _result(request, payload["rows"], source="core_redis", status="READY",
                                   version="redis_hot_view", available_at=payload.get("available_at"),
                                   quality={"coverage": len(payload["rows"]), "partial": False},
                                   lineage=[{"dataset": dataset, "hot_view": True}])
            except (OSError, ValueError, TypeError):
                pass
            manifest_path = self.catalog.locate(dataset, feature_date)
            if manifest_path is None or not manifest_path.is_file():
                return _result(request, [], source="core_data", status="PARTIAL",
                               reason="DAILY_REPORT_FEATURES_NOT_IMPORTED",
                               quality={"coverage": 0, "partial": True, "fallback": "daily_history"})
            try:
                manifest = reader.read_manifest(manifest_path)
                available = _parse_pit_timestamp(manifest.get("available_at"))
                if available is None or cutoff is None or available > cutoff:
                    return _result(request, [], source="core_data", status="PARTIAL",
                                   reason="DAILY_REPORT_FEATURES_NOT_PIT_READY",
                                   quality={"coverage": 0, "partial": True, "fallback": "daily_history"})
                rows = reader.read_rows(self.catalog, manifest_path)
            except (OSError, ValueError, TypeError, KeyError):
                rows, manifest = [], {}
            if not rows:
                return _result(request, [], source="core_data", status="PARTIAL",
                               reason="DAILY_REPORT_FEATURES_EMPTY",
                               quality={"coverage": 0, "partial": True, "fallback": "daily_history"})
            return _result(request, rows, source="core_data", status="READY",
                           version=str(manifest.get("version") or "unknown"),
                           available_at=manifest.get("available_at"),
                           quality={"coverage": len(rows), "partial": False},
                           lineage=[{"dataset": dataset, "source": "core_data"}])
        if request.slice_id == "daily_history":
            through_date = str(request.params.get("prior_trade_date") or date)
            from engine_core.daily_history import core_history_rows, read_daily_history_cache
            history_days = max(1, int(request.params.get("history_days", 5)))
            cached = read_daily_history_cache(self.redis, through_date, asof=request.asof,
                                              history_days=history_days)
            if cached is not None:
                rows, history_meta = cached
                history_meta["row_count"] = len(rows)
                return _result(request, rows, source="core_data", status="READY",
                               quality={"coverage": len(rows), "partial": False,
                                        "history_trade_dates": history_meta["history_trade_dates"],
                                        "field_units": history_meta["field_units"]},
                               version="redis_hot_view", available_at=history_meta["available_at"],
                               lineage=[{"dataset": "daily_history", "source": "core_data",
                                         "hot_view": True}])
            try:
                rows, history_meta = core_history_rows(self.catalog, through_date=through_date,
                                                       asof=request.asof, max_days=history_days)
            except (OSError, ValueError, TypeError):
                rows, history_meta = [], {"row_count": 0, "history_trade_dates": 0,
                                           "available_at": None, "unknown_availability": True}
            if not rows:
                compact_path = self._select_manifest(dataset, through_date, request.asof, reader=reader)
                if compact_path is not None and compact_path.is_file():
                    try:
                        compact_manifest = reader.read_manifest(compact_path)
                        compact_rows = reader.read_rows(self.catalog, compact_path)
                        for row in compact_rows:
                            row.setdefault("available_at", compact_manifest.get("available_at"))
                            row.setdefault("source", compact_manifest.get("source") or "core_data")
                        compact_rows, unknown = reader.pit_rows(compact_rows, request.asof)
                        if compact_rows:
                            return _result(request, compact_rows, source="core_data:daily_history_compact",
                                           status="PARTIAL" if unknown else "READY",
                                           reason="NO_ASOF" if unknown else None,
                                           quality={"coverage": len(compact_rows), "partial": unknown,
                                                    "legacy_core_dataset": True},
                                           version="daily_history_compact",
                                           available_at=compact_manifest.get("available_at"))
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                return _result(request, [], source="core_data", status="MISSING",
                               reason="DAILY_HISTORY_NOT_IMPORTED", quality={"coverage": 0})
            indicator_rows: dict[str, dict[str, Any]] = {}
            indicator_available_at = None
            indicator_date_used = None
            try:
                indicator_dates = sorted(day for day in self.catalog.read().get("datasets", {}).get("daily_indicators", {}).get("dates", {})
                                         if day <= through_date)
                cutoff = _parse_pit_timestamp(request.asof)
                for indicator_date in reversed(indicator_dates):
                    manifest_path = self.catalog.locate("daily_indicators", indicator_date)
                    if manifest_path is None or not manifest_path.is_file():
                        continue
                    manifest = reader.read_manifest(manifest_path)
                    available = _parse_pit_timestamp(manifest.get("available_at"))
                    if available is None or cutoff is None or available > cutoff:
                        continue
                    indicator_rows = {str(row.get("symbol", "")).zfill(6): row
                                      for row in reader.read_rows(self.catalog, manifest_path)}
                    indicator_available_at = manifest.get("available_at")
                    indicator_date_used = indicator_date
                    break
            except (AttributeError, OSError, ValueError, TypeError):
                indicator_rows = {}
            latest_bar_date = max(str(row.get("date", ""))[:10] for row in rows)
            indicator_fields = ("ma5", "ma10", "ma20", "bias_20", "rsi_6", "change_pct_5d",
                                "avg_turnover_5d", "limit_up_days_5", "vol_ratio")
            indicator_coverage = 0
            for row in rows:
                if str(row.get("date", ""))[:10] != latest_bar_date:
                    continue
                values = indicator_rows.get(str(row.get("symbol", "")).zfill(6))
                if indicator_date_used != latest_bar_date:
                    values = None  # PIT: never present an earlier day's indicators as the latest bar's
                if values:
                    indicator_coverage += 1
                for field in indicator_fields:
                    row[field] = values.get(field) if values else None
                if values:
                    row["indicator_source"] = "core_derived"
                    row["indicator_available_at"] = indicator_available_at
            status = "PARTIAL" if history_meta.get("unknown_availability") else "READY"
            reason = "DAILY_HISTORY_AVAILABILITY_UNKNOWN" if status == "PARTIAL" else None
            return _result(request, rows, source="core_data", status=status, reason=reason,
                           quality={"coverage": len(rows), "partial": status == "PARTIAL",
                                    "history_trade_dates": history_meta["history_trade_dates"],
                                    "indicator_coverage": indicator_coverage,
                                    "field_units": history_meta["field_units"]},
                           version="multi_date", available_at=history_meta.get("available_at"),
                           lineage=[{"dataset": "daily_kline", "source": source}
                                    for source in history_meta.get("sources", [])])
        if request.slice_id == "trading_calendar":
            calendar_manifest = self.catalog.locate(dataset, date or None)
            if calendar_manifest is None:
                from ..data_pipeline.trade_calendar import TradeCalendarStore
                store = TradeCalendarStore(self.catalog.root)
                try:
                    data, covered_date = store._covered(request.trade_date)
                except ValueError:
                    return _result(request, [], source="core_data:trade_calendar", status="MISSING",
                                   reason="CALENDAR_COVERAGE_UNKNOWN")
                rows = [{"date": item["d"]} for item in data["days"] if item["t"] and item["d"] <= covered_date]
                return _result(request, rows, source="core_data:trade_calendar",
                               status="READY", reason=None, version=data.get("last_ok_at"),
                               quality={"coverage_start": data.get("lit_from"),
                                        "coverage_end": data.get("lit_to"),
                                        "coverage": len(rows), "partial": False})
        if request.slice_id in {"hot_plates_yesterday", "yest_limit_pool"}:
            date = str(request.params.get("source_date") or "")
        if request.slice_id == "plate_members":
            date = str(request.params.get("source_date") or date)
        if request.slice_id == "limit_reason_history":
            try:
                dates = self.catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {})
                # Dataset dates are artifact/version dates, not the trading-day
                # lookback. Imported Kaipan history may be bundled under one
                # version date, so retain all PIT-eligible versions here; the
                # consumer applies the last-20-trading-session event window.
                eligible = sorted(day for day in dates if day <= request.trade_date)
                rows: list[dict[str, Any]] = []
                lineage = []
                unknown = False
                for day in eligible:
                    manifest_path = self._select_manifest(dataset, day, request.asof,
                                                          reader=reader, exact_date=True)
                    if manifest_path is None or not manifest_path.is_file():
                        continue
                    manifest = reader.read_manifest(manifest_path)
                    day_rows = reader.read_rows(self.catalog, manifest_path)
                    for row in day_rows:
                        row.setdefault("available_at", manifest.get("available_at"))
                        row.setdefault("dataset_date", day)
                    kept, day_unknown = reader.pit_rows(reader.as_rows(day_rows), request.asof)
                    unknown = unknown or day_unknown
                    rows.extend(kept)
                    lineage.append({"dataset": dataset, "trade_date": day,
                                    "available_at": manifest.get("available_at"),
                                    "available_at_trust": available_at_trust(manifest),
                                    "version": str(manifest.get("version") or "unknown")})
                unique = {}
                for row in rows:
                    key = (row.get("symbol", row.get("code")), row.get("Date", row.get("date")),
                           str(row.get("ZSCode", row.get("zs_code", ""))),
                           row.get("Reason", row.get("reason")))
                    unique[key] = row
                status = "PARTIAL" if unknown else "READY" if unique else "MISSING"
                return _result(request, list(unique.values()), source="core_data", status=status,
                               reason="NO_ASOF" if unknown else (None if unique else "DATASET_NOT_FOUND"),
                               version="multi_date", quality={"coverage": len(unique) or None,
                                                              "partial": unknown,
                                                              "history_trade_dates": len(lineage)},
                               lineage=lineage, pit_filtered=True)
            except (AttributeError, OSError, ValueError, KeyError, TypeError):
                return _result(request, [], source="core_data", status="MISSING", reason="CORRUPT_DATASET")
        if request.slice_id in {"hot_plates_yesterday", "yest_limit_pool"} and not date:
            return _result(request, [], source="core_data", status="MISSING",
                           reason="PREVIOUS_TRADE_DATE_MISSING")
        exact_date = request.slice_id in {"hot_plates_today", "hot_plates_yesterday", "hot_rank",
                                         "hot_plate_minutes",
                                         "yest_limit_pool", "stock_theme_assignment"}
        manifest_path = self._select_manifest(dataset, date or request.trade_date, request.asof,
                                              reader=reader, exact_date=exact_date)
        discovered_by = "artifact"
        if manifest_path is None and callable(getattr(self.redis, "get", None)):
            pointer_key = f"core:cache:data:{dataset}:{date or 'latest'}"
            try:
                raw = self.redis.get(pointer_key)
            except Exception:
                raw = None
            if raw:
                try:
                    pointer = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    manifest_path = Path(pointer["manifest"]) if pointer.get("manifest") else None
                    discovered_by = "core_redis"
                except (ValueError, TypeError, KeyError):
                    manifest_path = None
        if manifest_path is None or not manifest_path.is_file():
            if request.slice_id == "plate_amount_baseline":
                return _result(request, [], source="core_data", status="PARTIAL",
                               reason="PLATE_AMOUNT_BASELINE_UNAVAILABLE",
                               quality={"coverage": 0, "partial": True,
                                        "fallback": "UNKNOWN_NO_ON_DEMAND_RECOMPUTE"})
            return _result(request, [], source="core_data", status="MISSING", reason="DATASET_NOT_FOUND")
        try:
            manifest = reader.read_manifest(manifest_path)
            rows = reader.read_rows(self.catalog, manifest_path)
        except (OSError, ValueError, KeyError):
            return _result(request, [], source="core_data", status="MISSING", reason="CORRUPT_DATASET")
        rows = reader.as_rows(rows)
        if request.slice_id == "hot_plate_minutes":
            plate_code = str(request.params.get("plate_code") or "")
            if plate_code:
                rows = [row for row in rows if str(row.get("plate_code") or "") == plate_code]
        for row in rows:
            row.setdefault("available_at", manifest.get("available_at"))
        original_count = len(rows)
        rows, unknown = reader.pit_rows(rows, request.asof)
        source = "core_redis" if discovered_by == "core_redis" else "core_data"
        filtered = len(rows) < original_count
        manifest_quality = manifest.get("quality")
        partial_source = (manifest.get("business_status") == "PARTIAL" or
                          (isinstance(manifest_quality, Mapping) and
                           bool(manifest_quality.get("partial"))))
        used_date = str(manifest.get("trade_date", date))
        date_fallback = used_date != request.trade_date and not exact_date
        status = "PARTIAL" if unknown or filtered or partial_source or date_fallback else "READY"
        return _result(request, rows, source=source, status=status,
                       reason=("NO_ASOF" if unknown else "FUTURE_ROWS_FILTERED" if filtered
                               else "DATE_FALLBACK" if date_fallback else None),
                       version=str(manifest.get("version") or "unknown"),
                       available_at=manifest.get("available_at"),
                       quality={"coverage": len(rows) or None,
                                "missing": "NO_ASOF" if unknown else ("FUTURE_ROWS_FILTERED" if filtered else None),
                                "partial": unknown or filtered or partial_source},
                       pit_date=(manifest.get("point_in_time_date") or manifest.get("pit_date")),
                       lineage=[{"dataset": dataset, "requested_date": request.trade_date,
                                "used_date": used_date,
                                "available_at": manifest.get("available_at"),
                                "available_at_trust": available_at_trust(manifest)}])

    def _select_manifest(self, dataset: str, max_trade_date: str,
                         asof: str, *, reader: SliceReader,
                         exact_date: bool = False) -> Path | None:
        """Select the newest artifact version whose date and availability are PIT eligible."""
        try:
            dates = self.catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {})
            cutoff = _parse_pit_timestamp(asof)
            candidates = []
            for day, versions in dates.items():
                if (day != max_trade_date if exact_date else day > max_trade_date):
                    continue
                for entry in versions:
                    path = Path(entry["manifest"])
                    available = _parse_pit_timestamp(entry.get("available_at"))
                    if available is None:
                        try:
                            manifest = reader.read_manifest(path)
                        except (OSError, ValueError, KeyError):
                            continue
                        available = _parse_pit_timestamp(manifest.get("available_at"))
                    if available is None or cutoff is None or available > cutoff:
                        continue
                    candidates.append((day, available, path))
            if not candidates:
                return None
            return max(candidates, key=lambda item: (item[0], item[1]))[2]
        except (AttributeError, OSError, ValueError, TypeError):
            return None

    def _legacy(self, reader: SliceReader, request: SliceRequest) -> SliceResult:
        try:
            rows = self._legacy_rows(request)
        except Exception:
            # A read-only legacy source may be absent in offline runs. Keep the
            # provider chain fail-closed and report the missing slice.
            rows = None
        if rows is None:
            return _result(request, [], source="legacy_redis", status="MISSING", reason="LEGACY_KEY_MISSING")
        rows, unknown = reader.pit_rows(rows, request.asof)
        if request.slice_id == "daily_history":
            for row in rows:
                row.setdefault("available_at", None)
        source = "ths_hot_rank" if request.slice_id == "hot_rank" else "legacy_redis"
        result = _result(request, rows, source=source, status="PARTIAL" if unknown else "READY",
                       reason="NO_ASOF" if unknown else None,
                       quality={"coverage": len(rows) or None, "missing": "NO_ASOF" if unknown else None,
                                "partial": unknown})
        result.meta["legacy_dependency"] = True
        result.meta["status"] = "DEGRADED"
        result.meta.setdefault("reason", "LEGACY_SOURCE_FALLBACK")
        if request.slice_id == "daily_history":
            result.meta["reason"] = "LEGACY_DAILY_HISTORY_DEGRADED"
        return result

    def _legacy_rows(self, request: SliceRequest) -> list[dict[str, Any]] | None:
        redis = self.redis
        if redis is None:
            return None
        date = str(request.params.get("source_date") or request.trade_date)
        sid = request.slice_id
        if sid in {"hot_plates_yesterday", "yest_limit_pool"}:
            date = str(request.params.get("prior_trade_date") or request.params.get("source_date") or "")
            if not date:
                return None
        if sid == "stock_name_map":
            key = f"cache:stock_extra:{date}"
            if hasattr(redis, "hgetall"):
                value = redis.hgetall(key)
                if value:
                    return _hash_rows(value)
            return None
        if sid == "daily_history":
            prior = str(request.params.get("prior_trade_date") or "")
            key = f"cache:kline_ready:{prior}" if prior else ""
            symbols = list(redis.hkeys(key)) if key and hasattr(redis, "hkeys") else []
            if not symbols or not hasattr(redis, "pipeline"):
                return None
            pipe = redis.pipeline()
            for symbol in symbols:
                normalized_symbol = symbol.decode() if isinstance(symbol, bytes) else str(symbol)
                normalized_symbol = normalized_symbol.zfill(6)
                pipe.get(f"cache:kline:d:{normalized_symbol}")
            values = pipe.execute()
            import zlib
            rows: list[dict[str, Any]] = []
            for symbol, raw in zip(symbols, values):
                try:
                    normalized_symbol = symbol.decode() if isinstance(symbol, bytes) else str(symbol)
                    normalized_symbol = normalized_symbol.zfill(6)
                    blob = bytes.fromhex(raw.decode() if isinstance(raw, bytes) else raw)
                    history = json.loads(zlib.decompress(blob))
                    for item in history:
                        normalized = dict(item, symbol=normalized_symbol)
                        if "date" not in normalized and normalized.get("time") is not None:
                            normalized["date"] = str(normalized["time"])[:10]
                        if "amount_yuan" not in normalized and normalized.get("amount") is not None:
                            normalized["amount_yuan"] = normalized["amount"]
                        rows.append(normalized)
                except (ValueError, TypeError, zlib.error, json.JSONDecodeError):
                    continue
            return rows or None
        keys = {
            "yest_limit_pool": f"cache:yest_limit_pool:{date}",
            "hot_plates_today": f"cache:hot_plates:{date}",
            "hot_plates_yesterday": f"cache:hot_plates:{date}",
            "hot_rank": f"cache:hot_rank:{date}",
        }
        key = keys.get(sid)
        if not key:
            return None
        if hasattr(redis, "hgetall"):
            value = redis.hgetall(key)
            return _hash_rows(value) if value else None
        return None

    def _file(self, reader: SliceReader, request: SliceRequest) -> SliceResult:
        if not self.file_fallback:
            return _result(request, [], source="file", status="MISSING", reason="FILE_FALLBACK_DISABLED")
        path = request.params.get("fallback_file")
        if not path:
            return _result(request, [], source="file", status="MISSING", reason="NO_EXPLICIT_FALLBACK_FILE")
        file_path = Path(path)
        try:
            if file_path.suffix == ".json":
                payload = json.loads(file_path.read_text(encoding="utf-8"))
                rows = _as_rows(payload.get("rows", payload) if isinstance(payload, dict) else payload)
            else:
                import csv
                with file_path.open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
        except (OSError, ValueError, UnicodeError):
            return _result(request, [], source="FILE_FALLBACK", status="MISSING", reason="FILE_UNREADABLE")
        rows, unknown = reader.pit_rows(rows, request.asof)
        return _result(request, rows, source="FILE_FALLBACK", status="PARTIAL",
                       reason="NO_ASOF" if unknown else "EXPLICIT_FILE_FALLBACK",
                       quality={"coverage": len(rows) or None, "missing": "NO_ASOF" if unknown else None,
                                "partial": True})


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [dict(row, symbol=str(key).zfill(6)) if isinstance(row, dict) else
                {"symbol": str(key).zfill(6), "value": row} for key, row in value.items()]
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _hash_rows(value: Mapping[Any, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, raw in value.items():
        name = key.decode() if isinstance(key, bytes) else str(key)
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            item = json.loads(text)
            rows.append(dict(item, symbol=str(name).zfill(6)) if isinstance(item, dict)
                        else {"symbol": str(name).zfill(6), "value": item})
        except ValueError:
            rows.append({"symbol": str(name).zfill(6), "value": text})
    return rows


def _pit_rows(rows: list[dict[str, Any]], asof: str) -> tuple[list[dict[str, Any]], bool]:
    cutoff = _parse_pit_timestamp(asof)
    if cutoff is None:
        raise ValueError("slice_asof_timezone_required")
    parsed_cache: dict[Any, datetime | None] = {}

    def parse(value: Any) -> datetime | None:
        try:
            cached = parsed_cache.get(value, ...)
        except TypeError:
            return _parse_pit_timestamp(value)
        if cached is not ...:
            return cached
        parsed = _parse_pit_timestamp(value)
        if len(parsed_cache) >= 8192:
            parsed_cache.clear()
        parsed_cache[value] = parsed
        return parsed

    selected = []
    unknown = False
    for row in rows:
        event = next((row.get(key) for key in ("event_time", "event_date", "date")
                      if row.get(key) not in (None, "")), None)
        available = next((row.get(key) for key in
                          ("available_at", "observed_at", "created_at", "capture_time", "sampled_at")
                          if row.get(key) not in (None, "")), None)
        event_dt, available_dt = parse(event), parse(available)
        # Availability is the PIT boundary even if event time is absent or malformed.
        if available_dt is not None and available_dt > cutoff:
            continue
        if event_dt is not None and event_dt > cutoff:
            continue
        if event_dt is None and available_dt is None:
            unknown = True
        selected.append(row)
    return selected, unknown


def _canonical_hot_plate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use one PIT timestamp field for hot-plate records before filtering."""
    normalized = []
    for row in rows:
        item = dict(row)
        if not (item.get("code") or item.get("plate_code")) and item.get("symbol"):
            item["code"] = str(item["symbol"]).strip()
        sampled = next((item.get(key) for key in
                        ("available_at", "sampled_at", "captured_local", "capture_time")
                        if item.get(key) not in (None, "")), None)
        if sampled is not None:
            item["available_at"] = sampled
        normalized.append(item)
    return normalized


def _latest_hot_plate_rows(rows: list[dict[str, Any]], asof: str) -> list[dict[str, Any]]:
    """Return one latest as-of row per plate, preserving unknown-time rows last-seen."""
    latest: dict[str, tuple[datetime | None, int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        code = str(row.get("code") or row.get("plate_code") or row.get("symbol") or "").strip()
        if not code:
            continue
        cutoff = _parse_pit_timestamp(asof)
        sample_values = [row.get(key) for key in
                         ("sampled_at", "captured_local", "capture_time", "available_at")
                         if row.get(key) not in (None, "")]
        parsed_samples = [_parse_pit_timestamp(value) for value in sample_values]
        if cutoff is not None and any(stamp is not None and stamp > cutoff for stamp in parsed_samples):
            continue
        stamp = next((parsed for parsed in parsed_samples if parsed is not None), None)
        previous = latest.get(code)
        rank = (stamp, index)
        if previous is None or (stamp is not None and
                                (previous[0] is None or rank > (previous[0], previous[1]))) or (
                stamp is None and previous[0] is None and index > previous[1]):
            latest[code] = (stamp, index, row)
    return [item[2] for item in latest.values()]


def _parse_pit_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


__all__ = ["SliceReader", "SliceResult", "SliceProviders"]


def trading_dates_from_catalog(catalog: Any) -> list[str]:
    """Return observed trade dates from Core datasets; never infer weekdays."""
    try:
        datasets = catalog.read().get("datasets", {})
    except (AttributeError, OSError, ValueError):
        return []
    for name in ("daily_kline", "symbol_universe"):
        entry = datasets.get(name, {}) if isinstance(datasets, Mapping) else {}
        dates = entry.get("dates", {}) if isinstance(entry, Mapping) else {}
        observed = sorted(day for day in dates if isinstance(day, str) and len(day) == 10)
        if observed:
            return observed
    return []


def previous_trade_date(catalog: Any, trade_date: str) -> str | None:
    """Find the previous date backed by Core calendar data, or fail closed."""
    from ..trade_day_context import previous_trade_date as context_previous
    root = getattr(catalog, "root", None)
    if root is None:
        return None
    try:
        return context_previous(root, trade_date)
    except ValueError:
        return None


__all__ += ["trading_dates_from_catalog", "previous_trade_date"]
