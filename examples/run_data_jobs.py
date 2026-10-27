#!/usr/bin/env python3
"""Run injected Core data adapters. Importing this CLI never connects to a source."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
from engine_core.data_pipeline.registry import DATASETS, get_dataset
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.reader import load_dataset
from engine_core.data_pipeline.job_contract import (
    JobResult as ContractJobResult,
    emit as emit_job_result,
    run_with_degrade,
)


def _load_adapter(spec: str | None) -> dict[str, Any]:
    if not spec: return {}
    module_name, separator, factory_name = spec.partition(":")
    if not separator: raise ValueError("adapter must be module:function")
    factory = getattr(importlib.import_module(module_name), factory_name)
    value = factory()
    if not isinstance(value, dict): raise ValueError("adapter factory must return a mapping")
    return value


def _plate_codes(artifact_root: str, trade_date: str, configured: str | None = None) -> list[str]:
    candidates = ([Path(configured)] if configured else []) + [
        Path(artifact_root) / "plate_registry.json",
        Path(artifact_root) / "plate" / "registry.json",
        Path(artifact_root) / "plate_registry" / "registry.json",
    ]
    codes_path = next((path for path in candidates if path.is_file()), None)
    if codes_path is not None:
        payload = json.loads(codes_path.read_text(encoding="utf-8"))
        plate_rows = payload.get("plates", []) if isinstance(payload, dict) else payload
        return [str(item.get("plate_code", item.get("code", ""))
                    if isinstance(item, dict) else item)
                for item in plate_rows if isinstance(item, (dict, str, int))]
    catalog = ArtifactCatalog(artifact_root)
    path = catalog.locate("plate_registry", trade_date) or catalog.locate("plate_registry")
    if path is None:
        return []
    return list(dict.fromkeys(str(row.get("plate_code") or row.get("code") or "")
                              for row in catalog.read_rows(path)
                              if row.get("plate_code") or row.get("code")))


def _is_complete_member_manifest(manifest: dict[str, Any]) -> bool:
    if not isinstance(manifest, dict):
        return False
    quality = manifest.get("quality")
    if not isinstance(quality, dict) or quality.get("status") != "READY":
        return False
    if manifest.get("business_status") == "PARTIAL":
        return False
    try:
        if int(manifest.get("row_count", 0)) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    details = manifest.get("provider_details")
    if isinstance(details, dict) and (details.get("failed_plates") or
                                      details.get("unqueried_plate_count", 0)):
        return False
    return True


def _universe_delta(current: set[str], previous: set[str], *, reject_ratio: float = 0.10) -> tuple[int, float]:
    delta = len(current ^ previous)
    ratio = delta / max(1, len(previous))
    if ratio > reject_ratio:
        raise RuntimeError("quality_invalid:universe_change_over_10pct")
    return delta, ratio


def _normalize_universe_rows(source_rows: list[list[str]]) -> list[dict[str, str]]:
    """Normalize real query_all_stock tuples, keeping exchange-qualified identity."""
    by_symbol: dict[str, dict[str, str]] = {}
    possible_identities: dict[str, set[str]] = {}
    for source_row in source_rows:
        if len(source_row) < 3:
            continue
        code, status, name = map(str, source_row[:3])
        exchange, sep, symbol = code.partition(".")
        exchange, symbol = exchange.lower(), symbol.strip()
        if not sep or len(symbol) != 6 or not symbol.isdigit():
            continue
        eligible = ((exchange == "sh" and symbol.startswith(("60", "68"))) or
                    (exchange == "sz" and symbol.startswith(("00", "30"))))
        # 000xxx Shanghai codes are index families, including known collisions
        # with Shenzhen A shares; they must not be treated as competing stocks.
        plausible_stock_code = symbol.startswith(("60", "68", "00", "30")) and not (
            exchange == "sh" and symbol.startswith("000"))
        if plausible_stock_code:
            possible_identities.setdefault(symbol, set()).add(f"{exchange}.{symbol}")
        if not eligible:
            continue
        row = {"symbol": symbol, "code": f"{exchange}.{symbol}", "name": name,
               "trade_status": status, "universe_asof": "", "source": "baostock_query_all_stock"}
        previous = by_symbol.get(symbol)
        if previous is not None and previous["code"] != row["code"]:
            raise RuntimeError("quality_invalid:universe_identity_conflict")
        if previous is not None and previous != row:
            raise RuntimeError("quality_invalid:universe_duplicate_conflict")
        by_symbol[symbol] = row
    for symbol, codes in possible_identities.items():
        if symbol in by_symbol and len(codes) > 1:
            raise RuntimeError("quality_invalid:universe_identity_conflict")
    return [by_symbol[symbol] for symbol in sorted(by_symbol)]


def _symbols_file(path: str | Path, universe_rows: list[dict[str, Any]]) -> list[str]:
    """Read a bounded debug cohort and ensure every identifier belongs to the PIT universe."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"symbols_file_unreadable:{type(exc).__name__}") from None
    symbols: list[str] = []
    for line_number, raw in enumerate(lines, 1):
        value = raw.strip()
        if not value:
            continue
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"symbols_file_invalid_code:line={line_number}")
        if value in symbols:
            raise ValueError(f"symbols_file_duplicate_code:line={line_number}")
        symbols.append(value)
    if not symbols:
        raise ValueError("symbols_file_empty")
    available = {str(row.get("symbol", row.get("code", ""))).rsplit(".", 1)[-1].zfill(6)
                 for row in universe_rows}
    unknown = sorted(set(symbols) - available)
    if unknown:
        raise ValueError("symbols_file_outside_universe:" + ",".join(unknown[:5]))
    return symbols


def _require_sample_artifact_root(path: str | Path) -> str:
    root = Path(path).expanduser().resolve()
    production_root = Path(os.getenv("CORE_DATA_ROOT", "~/core-data")).expanduser().resolve()
    try:
        root.relative_to(production_root)
    except ValueError:
        return str(root)
    raise ValueError("symbols_file_artifact_root_cannot_be_core_data_root")


def _default_adapters(artifact_root: str, legacy_root: str, max_requests: int,
                      write_enabled: bool, *, baostock_timeout_seconds: float = 60.0,
                      baostock_total_timeout_seconds: float | None = None,
                      baostock_symbols_file: str | None = None) -> dict[str, Any]:
    """Construct lazy read-only adapters; optional SDK imports happen on fetch."""
    sample_path = Path(baostock_symbols_file).expanduser() if baostock_symbols_file else None
    if sample_path is not None:
        artifact_root = _require_sample_artifact_root(artifact_root)
    sample_token = (hashlib.sha256(sample_path.read_bytes()).hexdigest()[:12]
                    if sample_path is not None else None)

    def symbols_for(day: str) -> list[str]:
        found = load_dataset("symbol_universe", day, prefer="file",
                             catalog=ArtifactCatalog(artifact_root))
        if found is None:
            raise RuntimeError("symbol_universe_artifact_required:" + day)
        if sample_path is not None:
            return _symbols_file(sample_path, found.rows)
        return [str(row.get("symbol", row.get("code", ""))).zfill(6) for row in found.rows]

    def universe_fetch(day: str, context: Any = None) -> dict[str, Any]:
        """Build a PIT-safe target-day snapshot from the preceding session's exchange list."""
        from datetime import datetime
        if context is not None:
            context.request()
        try:
            from engine_core.trade_day_context import previous_trade_date as context_previous
            asof = context_previous(artifact_root, day)
        except ValueError:
            raise RuntimeError("symbol_universe_prior_trade_date_required:" + day)
        import baostock as bs
        from engine_core.daily_kline_capture import BaostockClient, call_with_timeout
        client = BaostockClient(bs, timeout_seconds=baostock_timeout_seconds)
        call_with_timeout(client.login, timeout_seconds=client.timeout_seconds)
        try:
            def read_all_stock():
                result = bs.query_all_stock(day=asof)
                if getattr(result, "error_code", "0") != "0":
                    raise RuntimeError("symbol_universe_source_failed")
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                return rows
            raw_rows = call_with_timeout(read_all_stock, timeout_seconds=client.timeout_seconds)
        finally:
            call_with_timeout(client.logout, timeout_seconds=client.timeout_seconds)
        rows = _normalize_universe_rows(raw_rows)
        for row in rows:
            row["universe_asof"] = asof
        symbols = [row["symbol"] for row in rows]
        if not rows: raise RuntimeError("quality_invalid:empty_response")
        if any(not row["name"] or not row["trade_status"] for row in rows):
            raise RuntimeError("schema_invalid:required_field_missing")
        delta_count = 0
        change = 0.0
        previous = load_dataset("symbol_universe", asof, prefer="file",
                                catalog=ArtifactCatalog(artifact_root))
        if previous is not None:
            old = {str(row["symbol"]) for row in previous.rows}
            delta_count, change = _universe_delta(set(symbols), old)
        return {"rows": rows, "expected_count": len(rows), "as_of": asof,
                "partial": change > 0.02,
                "universe_delta_count": delta_count,
                "available_at": datetime.now().astimezone().isoformat()}

    class DailyKlineBaostockAdapter:
        def __init__(self):
            self.total_timeout_seconds = baostock_total_timeout_seconds

        def __call__(self, day: str, context: Any = None) -> Any:
            return self.fetch(day, context)

        def fetch(self, day: str, context: Any = None) -> Any:
            from engine_core.daily_kline_capture import BaostockClient
            from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
            checkpoint = (Path(artifact_root) / ".checkpoints" / f"daily_kline-{day}.json"
                          if write_enabled or sample_path is not None else None)
            if checkpoint is not None and sample_token is not None:
                checkpoint = checkpoint.with_name(f"daily_kline-{day}-sample-{sample_token}.json")

            def client_factory() -> Any:
                import baostock as bs
                return BaostockClient(bs)

            provider = BaostockDailyProvider(
                client_factory=client_factory, symbols=symbols_for,
                checkpoint_path=checkpoint, timeout_seconds=baostock_timeout_seconds,
                total_timeout_seconds=self.total_timeout_seconds, sample_scope=sample_path is not None,
            )
            result = provider.fetch(day, context)
            universe = load_dataset("symbol_universe", day, prefer="file",
                                    catalog=ArtifactCatalog(artifact_root))
            if universe is None:
                raise RuntimeError("symbol_universe_artifact_required:" + day)
            identity = {str(row.get("symbol", "")).zfill(6): row
                        for row in universe.rows if row.get("symbol")}
            for row in result.get("rows", []):
                source = identity.get(str(row.get("symbol", "")).zfill(6))
                if source is not None:
                    row["code"] = source.get("code")
                    row["universe_version"] = universe.manifest.get("version")
            return result

    daily_fetch = DailyKlineBaostockAdapter()

    class LazyKaipanClient:
        def _sdk(self):
            return importlib.import_module("pykaipan.pykaipan")

        def get_his_bans(self, day: str, level: int) -> Any:
            return self._sdk().getHisBans(date=day, ban=str(level), size="2000")

        def get_his_plate_ids(self, plate: str, day: str, index: int, size: int) -> Any:
            # Keep pagination parameters explicit, matching the pykaipan protocol.
            api = self._sdk()
            return api.getHisPlateIds(p=plate, date=day, index=str(index), size=str(size))

        def get_his_plates(self, day: str, index: int, size: int) -> Any:
            return self._sdk().getHisPlates(date=day, index=str(index), size=str(size))

        def get_his_plate_mins(self, plate: str, day: str) -> Any:
            return self._sdk().getHisPlateMins(s=plate, date=day)

        def get_ban_reasons(self, symbol: str) -> Any:
            return self._sdk().getBanReasons(code=symbol)

    def kaipan_provider(day: str):
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        codes = _plate_codes(artifact_root, day, os.getenv("CORE_PLATE_REGISTRY"))
        return KaipanProvider(LazyKaipanClient(), plate_codes=codes, max_requests=max_requests)

    # Factories defer SDK construction and all requests until JobRunner invokes them.
    def pool_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        return KaipanProvider(LazyKaipanClient(), max_requests=max_requests).limit_up_pool(day, context=context)

    def hot_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        return KaipanProvider(LazyKaipanClient(), max_requests=max_requests).hot_plates(day, context=context)

    def members_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        codes = _plate_codes(artifact_root, day, os.getenv("CORE_PLATE_REGISTRY"))
        checkpoint = Path(artifact_root) / ".checkpoints" / f"plate_members-{day}.json" if write_enabled else None
        provider = KaipanProvider(LazyKaipanClient(), plate_codes=codes,
                                  max_requests=max(max_requests, len(codes) * 8),
                                  min_interval_seconds=0.2,
                                  members_checkpoint_path=checkpoint)
        result = provider.plate_members(day, context=context, concurrency=4)
        available = (date.fromisoformat(day) + timedelta(days=1)).isoformat() + "T00:00:00+08:00"
        return {**result, "available_at": available}

    def reasons_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        checkpoint = Path(artifact_root) / ".checkpoints" / f"limit_reasons-{day}.json" if write_enabled else None
        pool = load_dataset("limit_up_pool", day, prefer="file",
                            catalog=ArtifactCatalog(artifact_root))
        if pool is None:
            raise RuntimeError("limit_up_pool_artifact_required:" + day)
        symbols = sorted({str(row.get("symbol", row.get("code", ""))).zfill(6)
                          for row in pool.rows if row.get("symbol") or row.get("code")})
        if not symbols:
            raise RuntimeError("limit_up_pool_has_no_symbols:" + day)
        provider = KaipanProvider(LazyKaipanClient(), symbols=symbols,
                                  max_requests=max(max_requests, len(symbols)),
                                  reasons_checkpoint_path=checkpoint)
        return provider.limit_reasons(day, context=context)

    return {"symbol_universe": universe_fetch, "daily_kline": daily_fetch, "limit_up_pool": pool_fetch,
            "plate_members": members_fetch, "hot_plates": hot_fetch,
            "limit_reasons": reasons_fetch}


def _call_universe_fetch(fetch: Any, trade_date: str) -> Any:
    """Call adapter factories that may optionally accept a fetch context."""
    try:
        accepts_context = len(inspect.signature(fetch).parameters) >= 2
    except (TypeError, ValueError):
        accepts_context = False
    return fetch(trade_date, None) if accepts_context else fetch(trade_date)


def _universe_payload_quality(payload: Any) -> bool:
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    symbols: set[str] = set()
    required = ("symbol", "name", "trade_status", "universe_asof")
    for row in rows:
        if not isinstance(row, dict) or any(row.get(key) in (None, "") for key in required):
            return False
        symbol = str(row["symbol"])
        if symbol in symbols:
            return False
        symbols.add(symbol)
    return True


def _carried_forward_universe(artifact_root: str, trade_date: str) -> dict[str, Any]:
    """Load the nearest valid published universe from the preceding five sessions."""
    from engine_core.trade_day_context import previous_trade_date

    cursor = trade_date
    lower_bound = None
    for _ in range(5):
        cursor = previous_trade_date(artifact_root, cursor)
        lower_bound = cursor
    catalog = ArtifactCatalog(artifact_root)
    indexed_days = catalog.read().get("datasets", {}).get("symbol_universe", {}).get("dates", {})
    candidates = sorted((day for day in indexed_days
                         if lower_bound is not None and lower_bound <= day < trade_date), reverse=True)
    for source_day in candidates:
        path = catalog.locate("symbol_universe", source_day)
        if path is None:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            rows = catalog.read_rows(path)
        except (OSError, ValueError, TypeError):
            continue
        quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
        if (quality.get("status") == "FAILED" or
                not isinstance(manifest.get("version"), str) or not rows):
            continue
        universe_asof = manifest.get("universe_asof") or manifest.get("as_of")
        if not universe_asof:
            universe_asof = next((row.get("universe_asof") for row in rows
                                  if isinstance(row, dict) and row.get("universe_asof")), None)
        payload = {"rows": rows, "expected_count": len(rows), "coverage_count": len(rows),
                   "as_of": universe_asof, "universe_asof": universe_asof,
                   "carried_forward_from": source_day, "tier": 1, "degraded": True,
                   "quality_status": "DEGRADED", "partial": True}
        if universe_asof and _universe_payload_quality(payload):
            return payload
    raise RuntimeError("quality_invalid:no_recent_symbol_universe")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, choices=["all", *DATASETS.keys()])
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--write", action="store_true", help="publish bounded views to Core Redis")
    parser.add_argument("--td-write", action="store_true", help="write through an injected independent TD adapter")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="fetch and validate without persisting (default)")
    parser.add_argument("--execute", action="store_true", help="run fetch and commit the artifact version")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--baostock-timeout-seconds", type=float, default=60.0,
                        help="hard caller wait for each Baostock operation")
    parser.add_argument("--baostock-total-timeout-seconds", type=float,
                        help="optional wall-clock budget for the complete daily_kline fetch")
    parser.add_argument("--budget-seconds", type=float,
                        default=os.getenv("CORE_DAILY_KLINE_BUDGET_SECONDS") or 240.0,
                        help="wall-clock budget for the complete daily_kline provider chain")
    parser.add_argument("--baostock-budget-fraction", type=float, default=0.6,
                        help="fraction of the chain budget reserved for Baostock")
    parser.add_argument("--backfill-days", type=int, default=10,
                        help="plate_members trading dates, newest first")
    parser.add_argument("--providers",
                        help="daily_kline source set; fixed quality order is baostock,wencai,q2_final")
    parser.add_argument("--symbols-file", help="debug-only one-symbol-per-line Baostock cohort")
    parser.add_argument("--adapter", help="injected module:function returning dataset fetch callables")
    parser.add_argument("--artifact-root", help="artifact root (required with --symbols-file)")
    parser.add_argument("--td-database", help="allowlisted independent database: core_data/core_intraday")
    args = parser.parse_args(argv)
    artifact_root_explicit = args.artifact_root is not None
    args.artifact_root = args.artifact_root or os.getenv("CORE_DATA_ROOT", "~/core-data")
    args.artifact_root = str(Path(args.artifact_root).expanduser())
    if args.symbols_file and args.job != "daily_kline":
        parser.error("--symbols-file is only valid with --job daily_kline")
    if args.symbols_file:
        if args.write or args.td_write:
            parser.error("--symbols-file cannot publish to Redis or TD")
        if not artifact_root_explicit:
            parser.error("--symbols-file requires an explicit --artifact-root")
        try:
            args.artifact_root = _require_sample_artifact_root(args.artifact_root)
        except ValueError as exc:
            parser.error(str(exc))
    if args.providers is None:
        args.providers = "baostock" if args.symbols_file else "baostock,wencai,q2_final"
    if args.td_write and (not args.write or not args.td_database):
        parser.error("--td-write requires --write and --td-database")
    if args.budget_seconds <= 0:
        parser.error("--budget-seconds must be positive")
    if not 0 < args.baostock_budget_fraction <= 1:
        parser.error("--baostock-budget-fraction must be greater than 0 and at most 1")
    names = list(DATASETS) if args.job == "all" else [args.job]
    adapters = _default_adapters(args.artifact_root,
        "",
        args.max_requests, args.write or args.execute,
        baostock_timeout_seconds=args.baostock_timeout_seconds,
        baostock_total_timeout_seconds=args.baostock_total_timeout_seconds,
        baostock_symbols_file=args.symbols_file)
    adapters.update(_load_adapter(args.adapter))
    daily_providers = [name.strip() for name in args.providers.split(",") if name.strip()]
    if args.symbols_file and daily_providers != ["baostock"]:
        parser.error("--symbols-file requires --providers baostock")
    unknown_providers = set(daily_providers) - {"baostock", "wencai", "q2_final"}
    if unknown_providers:
        parser.error("unknown daily provider: " + ",".join(sorted(unknown_providers)))
    if "wencai" in daily_providers and args.max_requests > 15:
        args.max_requests = 15
    missing = [name for name in names if name not in adapters and name != "daily_kline"]
    if missing: raise RuntimeError("fetcher_not_configured:" + ",".join(missing))
    setup_started = time.monotonic()
    try:
        redis = None
        if args.write:
            import redis as redis_package
            redis = redis_package.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        td_adapter = adapters.get("td_writer") if args.td_write else None
        runner = JobRunner(artifact_root=args.artifact_root, redis=redis, td_adapter=td_adapter,
                           budget=RequestBudget(max_requests=args.max_requests))
    except Exception as exc:
        if args.job != "symbol_universe":
            raise
        primary_source = get_dataset("symbol_universe").source
        result = ContractJobResult(
            contract=1, job="symbol_universe", trade_date=args.date, status="failed",
            tier=0, source=primary_source, dataset="symbol_universe", version=None,
            row_count=None, coverage=None, error_code="EXECUTION_SETUP_FAILED", retryable=True,
            attempts=0, elapsed_ms=round((time.monotonic() - setup_started) * 1000, 3),
            detail={"exception_type": type(exc).__name__})
        return emit_job_result(result)
    if args.job == "symbol_universe":
        started = time.monotonic()
        primary_source = get_dataset("symbol_universe").source
        result: ContractJobResult | None = None
        try:
            primary_fetch = adapters["symbol_universe"]
            result, payload = run_with_degrade(
                "symbol_universe", args.date,
                [(0, primary_source, lambda day: _call_universe_fetch(primary_fetch, day)),
                 (1, "carried_forward", lambda day: _carried_forward_universe(
                     args.artifact_root, day))],
                dataset="symbol_universe", quality_check=_universe_payload_quality)
            if payload is None:
                return emit_job_result(replace(
                    result, elapsed_ms=round((time.monotonic() - started) * 1000, 3)))
            payload = {**payload, "tier": result.tier, "degraded": result.tier >= 1}
            stored = runner.run(
                "symbol_universe", args.date, fetch=lambda _day: payload,
                publish=args.write, td_write=args.td_write, source=result.source,
                as_of=payload.get("as_of"),
                dry_run=not (args.write or args.execute))
            result = replace(
                result,
                version=stored.manifest.get("version"),
                row_count=stored.manifest.get("row_count"),
                coverage=stored.manifest.get("quality", {}).get("coverage"),
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                detail={**(result.detail or {}),
                        **({"carried_forward_from": payload["carried_forward_from"]}
                           if payload.get("carried_forward_from") else {})})
            return emit_job_result(result)
        except Exception as exc:
            message = str(exc)
            normalized = message.lower()
            if isinstance(exc, TimeoutError) or "timeout" in normalized:
                error_code, retryable = "BAOSTOCK_TIMEOUT", True
            elif normalized.startswith("quality_invalid:"):
                suffix = message.split(":", 1)[1]
                error_code, retryable = suffix.upper() or "QUALITY_INVALID", False
            elif normalized.startswith("schema_invalid:"):
                suffix = message.split(":", 1)[1]
                error_code, retryable = suffix.upper() or "SCHEMA_INVALID", False
            else:
                error_code, retryable = "EXECUTION_FAILED", True
            failed_tier = result.tier if result is not None else 0
            failed_source = result.source if result is not None else primary_source
            failed_attempts = result.attempts if result is not None else 1
            failed = ContractJobResult(
                contract=1, job="symbol_universe", trade_date=args.date, status="failed",
                tier=failed_tier, source=failed_source, dataset="symbol_universe", version=None,
                row_count=None, coverage=None, error_code=error_code, retryable=retryable,
                attempts=failed_attempts, elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                detail={"exception_type": type(exc).__name__})
            return emit_job_result(failed)
    if args.job == "plate_members":
        if args.backfill_days < 1:
            parser.error("--backfill-days must be at least 1")
        catalog = ArtifactCatalog(args.artifact_root)
        trading_dates = sorted((day for day in catalog.read().get("datasets", {})
                                .get("daily_kline", {}).get("dates", {}) if day <= args.date), reverse=True)
        target_dates = trading_dates[:args.backfill_days]
        if len(target_dates) < args.backfill_days:
            parser.error(f"need {args.backfill_days} daily_kline trading dates on or before {args.date}; found {len(target_dates)}")
        results = []
        for target_date in target_dates:
            existing = catalog.locate("plate_members", target_date)
            if existing is not None:
                manifest = json.loads(existing.read_text(encoding="utf-8"))
                if _is_complete_member_manifest(manifest):
                    line = {"job": "plate_members", "date": target_date,
                            "status": "SKIPPED_EXISTING", "row_count": manifest["row_count"],
                            "failed_plates": []}
                    print(json.dumps(line, sort_keys=True))
                    results.append(line)
                    continue
            available_at = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat() + "T00:00:00+08:00"
            result = runner.run("plate_members", target_date, fetch=adapters["plate_members"],
                                publish=args.write, td_write=args.td_write,
                                source=get_dataset("plate_members").source,
                                available_at=available_at,
                                dry_run=not (args.write or args.execute))
            line = {"job": "plate_members", "date": target_date,
                    "status": result.manifest["quality"]["status"],
                    "row_count": result.manifest["row_count"],
                    "failed_plates": result.manifest.get("provider_details", {}).get("failed_plates", []),
                    "storage_uri": result.manifest.get("storage_uri"),
                    "cache_publish": result.manifest.get("cache_publish")}
            print(json.dumps(line, sort_keys=True))
            results.append(line)
        print(json.dumps({"job": "plate_members", "dates": len(results),
                          "total_rows": sum(item.get("row_count", 0) for item in results),
                          "failed_plate_count": sum(len(item.get("failed_plates", [])) for item in results)},
                         sort_keys=True))
        return 0
    daily_kline_exit_code = 0
    for name in names:
        if name == "daily_kline":
            daily_started = time.monotonic()
            from engine_core.data_pipeline.providers.daily_kline_chain import (
                DailyKlineProviderChain, Q2FinalProvider, WencaiDailyProvider,
            )

            configured: dict[str, Any] = {}
            if "baostock" in daily_providers:
                configured["baostock"] = adapters[name]
            if "wencai" in daily_providers:
                wencai_fetch = adapters.get("wencai_daily_kline")
                if wencai_fetch is not None:
                    universe = load_dataset("symbol_universe", args.date, prefer="file",
                                            catalog=ArtifactCatalog(args.artifact_root))
                    expected_count = len(universe.rows) if universe is not None else None
                    configured["wencai"] = WencaiDailyProvider(
                        wencai_fetch, state_path=Path(args.artifact_root) / ".provider_state" / "wencai.json",
                        max_requests=args.max_requests, expected_count=expected_count,
                    )
                else:
                    configured["wencai"] = lambda _day: {
                        "status": "PROVIDER_UNAVAILABLE", "reason": "wencai_adapter_not_configured", "rows": []}
            if "q2_final" in daily_providers:
                q2_redis = adapters.get("q2_redis")
                if q2_redis is None:
                    redis_url = os.getenv("LEGACY_REDIS_URL") or os.getenv("CORE_REDIS_URL")
                    if redis_url:
                        import redis as redis_package
                        q2_redis = redis_package.Redis.from_url(redis_url, decode_responses=False)
                configured["q2_final"] = (Q2FinalProvider(q2_redis) if q2_redis is not None else
                    lambda _day: {"status": "PROVIDER_UNAVAILABLE", "reason": "q2_redis_not_configured", "rows": []})
            chain = DailyKlineProviderChain(providers=configured, artifact_root=args.artifact_root)
            result = chain.fetch_chain(
                args.date, persist=bool(args.execute or args.write),
                budget_seconds=args.budget_seconds,
                baostock_budget_fraction=args.baostock_budget_fraction)
            if result is None:
                print(json.dumps({"job": name, "status": "PROVIDER_UNAVAILABLE_OR_COVERAGE_LOW",
                                  "providers": daily_providers, "mode": "DRY_RUN" if not (args.execute or args.write) else "WRITE"}, sort_keys=True))
                daily_contract = ContractJobResult(
                    contract=1, job=name, trade_date=args.date, status="failed", tier=0,
                    source=None, dataset=name, version=None, row_count=None, coverage=None,
                    error_code="DAILY_KLINE_ALL_PROVIDERS_FAILED", retryable=True,
                    attempts=len(daily_providers),
                    elapsed_ms=round((time.monotonic() - daily_started) * 1000, 3),
                    detail={"providers": daily_providers})
                daily_kline_exit_code = emit_job_result(daily_contract)
                if args.job != "all":
                    return daily_kline_exit_code
                continue
            print(json.dumps({"job": name, "version": result.manifest.get("version"),
                              "source": result.manifest.get("source"), "degraded": result.manifest.get("degraded"),
                              "quality": result.manifest["quality"], "provider_attempts": result.manifest["provider_attempts"],
                              "storage_uri": result.manifest.get("storage_uri"),
                              "mode": "DRY_RUN" if not (args.execute or args.write) else "WRITE"}, sort_keys=True))
            sample_only = result.manifest.get("sample_scope") == "symbols_file"
            degraded = result.manifest.get("provider") != "baostock" or sample_only
            daily_contract = ContractJobResult(
                contract=1, job=name, trade_date=args.date,
                status="degraded" if degraded else "ok",
                tier=result.manifest.get("tier", 1 if result.manifest.get("provider") != "baostock" else 0),
                source=str(result.manifest.get("provider") or result.manifest.get("source") or "UNKNOWN"),
                dataset=name, version=result.manifest.get("version"),
                row_count=len(result.rows), coverage=result.manifest.get("quality", {}).get("coverage"),
                error_code=None, retryable=False,
                attempts=len(result.manifest.get("provider_attempts", [])),
                elapsed_ms=round((time.monotonic() - daily_started) * 1000, 3),
                detail={"business_status": result.manifest.get("business_status"),
                        "sample_scope": result.manifest.get("sample_scope"),
                        "provider_attempts": result.manifest.get("provider_attempts", [])})
            daily_kline_exit_code = emit_job_result(daily_contract)
            if args.job != "all":
                return daily_kline_exit_code
            continue
        result = runner.run(name, args.date, fetch=adapters[name], publish=args.write,
                            td_write=args.td_write, source=get_dataset(name).source,
                            dry_run=not (args.write or args.execute))
        print(json.dumps({"job": name, "version": result.manifest.get("version"),
                          "row_count": result.manifest["row_count"],
                          "sha256": result.manifest.get("sha256"),
                          "quality": result.manifest["quality"],
                          "storage_uri": result.manifest["storage_uri"],
                          "cache_publish": result.manifest["cache_publish"],
                          "mode": result.manifest.get("mode", "WRITE")}, sort_keys=True))
    return daily_kline_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
