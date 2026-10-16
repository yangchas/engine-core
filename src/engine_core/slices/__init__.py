"""Small, read-only data slice API used by report entry points."""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

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
                rows, unknown = _pit_rows(result.rows, request.asof)
                available_at = result.meta.get("available_at")
                if available_at:
                    parsed, cutoff = _parse_pit_timestamp(available_at), _parse_pit_timestamp(request.asof)
                    if parsed is None or cutoff is None or parsed > cutoff:
                        continue
                meta = dict(result.meta)
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
            pit_date: str | None = None) -> SliceResult:
    meta = {"slice_id": request.slice_id, "trade_date": request.trade_date,
            "asof": request.asof, "source": source, "version": version,
            "built_at": datetime.now().astimezone().isoformat(), "available_at": available_at,
            "quality": dict(quality or {"coverage": None, "missing": reason, "partial": False}),
            "status": status, "lineage": lineage or [], "snapshot_id": "report"}
    if pit_date:
        meta["pit_date"] = pit_date
    if reason:
        meta["reason"] = reason
    return SliceResult.create(rows, meta)


class SliceProviders:
    """Core artifact -> read-only legacy Redis -> explicit opt-in file adapters."""

    DATASETS = {
        "stock_name_map": "stock_static", "plate_registry": "plate_registry",
        "plate_members": "plate_members", "daily_history": "daily_history_compact",
        "auction_baseline": "auction_baseline", "yest_limit_pool": "limit_up_pool",
        "limit_reason_history": "limit_reasons", "hot_plates_today": "hot_plates",
        "hot_plates_yesterday": "hot_plates", "hot_rank": "hot_rank",
        "trading_calendar": "trade_calendar", "path_stats": "path_stats",
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
        if request.slice_id == "trading_calendar":
            calendar_manifest = self.catalog.locate(dataset, date or None)
            if calendar_manifest is None:
                dates = trading_dates_from_catalog(self.catalog)
                if dates:
                    rows = [{"date": day} for day in dates if day <= request.trade_date]
                    return _result(request, rows, source="core_data:daily_kline_or_symbol_universe",
                                   status="PARTIAL", reason="CALENDAR_DERIVED_FROM_CORE_DATASETS",
                                   version="derived", quality={"coverage": len(rows), "missing": None,
                                                                "partial": True})
                return _result(request, [], source="core_data", status="MISSING",
                               reason="CALENDAR_DATA_UNAVAILABLE")
        if request.slice_id in {"hot_plates_yesterday", "yest_limit_pool"}:
            date = str(request.params.get("source_date") or "")
        if request.slice_id == "daily_history":
            date = str(request.params.get("prior_trade_date") or date or "")
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
                    manifest_path = self.catalog.locate(dataset, day)
                    if manifest_path is None or not manifest_path.is_file():
                        continue
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    day_rows = self.catalog.read_rows(manifest_path)
                    for row in day_rows:
                        if not row.get("available_at") and manifest.get("available_at"):
                            row["available_at"] = manifest["available_at"]
                        row.setdefault("dataset_date", day)
                    kept, day_unknown = _pit_rows(_as_rows(day_rows), request.asof)
                    unknown = unknown or day_unknown
                    rows.extend(kept)
                    lineage.append({"dataset": dataset, "trade_date": day,
                                    "available_at": manifest.get("available_at")})
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
                               lineage=lineage)
            except (AttributeError, OSError, ValueError, KeyError, TypeError):
                return _result(request, [], source="core_data", status="MISSING", reason="CORRUPT_DATASET")
        if request.slice_id in {"hot_plates_yesterday", "yest_limit_pool"} and not date:
            return _result(request, [], source="core_data", status="MISSING",
                           reason="PREVIOUS_TRADE_DATE_MISSING")
        manifest_path = self.catalog.locate(dataset, date or None)
        if manifest_path is None and request.slice_id in {
                "stock_name_map", "plate_registry", "plate_members", "path_stats"}:
            try:
                dates = self.catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {})
                eligible = [day for day in dates if day <= request.trade_date]
                if eligible:
                    manifest_path = self.catalog.locate(dataset, max(eligible))
            except (AttributeError, OSError, ValueError):
                manifest_path = None
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
            return _result(request, [], source="core_data", status="MISSING", reason="DATASET_NOT_FOUND")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = self.catalog.read_rows(manifest_path)
        except (OSError, ValueError, KeyError):
            return _result(request, [], source="core_data", status="MISSING", reason="CORRUPT_DATASET")
        rows = _as_rows(rows)
        original_count = len(rows)
        rows, unknown = _pit_rows(rows, request.asof)
        source = "core_redis" if discovered_by == "core_redis" else "core_data"
        filtered = len(rows) < original_count
        status = "PARTIAL" if unknown or filtered or manifest.get("business_status") == "PARTIAL" else "READY"
        return _result(request, rows, source=source, status=status,
                       reason="NO_ASOF" if unknown else ("FUTURE_ROWS_FILTERED" if filtered else None),
                       version=str(manifest.get("version") or "unknown"),
                       available_at=manifest.get("available_at"),
                       quality={"coverage": len(rows) or None,
                                "missing": "NO_ASOF" if unknown else ("FUTURE_ROWS_FILTERED" if filtered else None),
                                "partial": unknown or filtered},
                       pit_date=(manifest.get("point_in_time_date") or manifest.get("pit_date")),
                       lineage=[{"dataset": dataset,
                                "available_at": manifest.get("available_at")}])

    def _legacy(self, reader: SliceReader, request: SliceRequest) -> SliceResult:
        try:
            rows = self._legacy_rows(request)
        except Exception:
            # A read-only legacy source may be absent in offline runs. Keep the
            # provider chain fail-closed and report the missing slice.
            rows = None
        if rows is None:
            return _result(request, [], source="legacy_redis", status="MISSING", reason="LEGACY_KEY_MISSING")
        rows, unknown = _pit_rows(rows, request.asof)
        source = "ths_hot_rank" if request.slice_id == "hot_rank" else "legacy_redis"
        result = _result(request, rows, source=source, status="PARTIAL" if unknown else "READY",
                       reason="NO_ASOF" if unknown else None,
                       quality={"coverage": len(rows) or None, "missing": "NO_ASOF" if unknown else None,
                                "partial": unknown})
        result.meta["legacy_dependency"] = True
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
        rows, unknown = _pit_rows(rows, request.asof)
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
    selected = []
    unknown = False
    for row in rows:
        event = next((row.get(key) for key in ("event_time", "event_date", "date")
                      if row.get(key) not in (None, "")), None)
        available = next((row.get(key) for key in
                          ("available_at", "observed_at", "created_at", "capture_time", "sampled_at")
                          if row.get(key) not in (None, "")), None)
        event_dt, available_dt = _parse_pit_timestamp(event), _parse_pit_timestamp(available)
        # Availability is the PIT boundary even if event time is absent or malformed.
        if available_dt is not None and available_dt > cutoff:
            continue
        if event_dt is not None and event_dt > cutoff:
            continue
        if event_dt is None and available_dt is None:
            unknown = True
        selected.append(row)
    return selected, unknown


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
    eligible = [day for day in trading_dates_from_catalog(catalog) if day < trade_date]
    return eligible[-1] if eligible else None


__all__ += ["trading_dates_from_catalog", "previous_trade_date"]
