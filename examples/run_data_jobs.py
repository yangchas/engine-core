#!/usr/bin/env python3
"""Run injected Core data adapters. Importing this CLI never connects to a source."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
from engine_core.data_pipeline.registry import DATASETS, get_dataset
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.reader import load_dataset


def _load_adapter(spec: str | None) -> dict[str, Any]:
    if not spec: return {}
    module_name, separator, factory_name = spec.partition(":")
    if not separator: raise ValueError("adapter must be module:function")
    factory = getattr(importlib.import_module(module_name), factory_name)
    value = factory()
    if not isinstance(value, dict): raise ValueError("adapter factory must return a mapping")
    return value


def _default_adapters(artifact_root: str, legacy_root: str, max_requests: int,
                      write_enabled: bool) -> dict[str, Any]:
    """Construct lazy read-only adapters; optional SDK imports happen on fetch."""
    def symbols_for(day: str) -> list[str]:
        found = load_dataset("symbol_universe", day, prefer="file",
                             catalog=ArtifactCatalog(artifact_root))
        if found is None:
            raise RuntimeError("symbol_universe_artifact_required:" + day)
        return [str(row.get("symbol", row.get("code", ""))).zfill(6) for row in found.rows]

    def daily_fetch(day: str, context: Any = None) -> Any:
        from engine_core.daily_kline_capture import BaostockClient
        from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
        checkpoint = Path(artifact_root) / ".checkpoints" / f"daily_kline-{day}.json" if write_enabled else None
        provider = BaostockDailyProvider(client_factory=BaostockClient, symbols=symbols_for,
                                         checkpoint_path=checkpoint)
        return provider.fetch(day, context)

    class LazyKaipanClient:
        def _analyzer(self):
            from examples.legacy_adapters import kaipan_analyzer
            return kaipan_analyzer(legacy_root)

        def get_his_bans(self, day: str, level: int) -> Any:
            return self._analyzer()._call_api("getHisBans", date=day, ban=str(level), size="2000")

        def get_his_plate_ids(self, plate: str, day: str, index: int, size: int) -> Any:
            # Keep pagination parameters explicit, matching the pykaipan protocol.
            from examples.legacy_adapters import _insert_legacy_root, kaipan_pykaipan_module
            _insert_legacy_root(legacy_root)
            api = kaipan_pykaipan_module()
            return api.getHisPlateIds(p=plate, date=day, index=str(index), size=str(size))

        def get_his_plates(self, day: str, index: int, size: int) -> Any:
            from examples.legacy_adapters import _insert_legacy_root, kaipan_pykaipan_module
            _insert_legacy_root(legacy_root)
            return kaipan_pykaipan_module().getHisPlates(date=day, index=str(index), size=str(size))

        def get_ban_reasons(self, symbol: str) -> Any:
            return self._analyzer().get_ban_reasons(symbol)

    def kaipan_provider(day: str):
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        configured = os.getenv("CORE_PLATE_REGISTRY")
        candidates = ([Path(configured)] if configured else []) + [
            Path(artifact_root) / "plate_registry.json",
            Path(artifact_root) / "plate" / "registry.json",
            Path(artifact_root) / "plate_registry" / "registry.json",
        ]
        codes_path = next((path for path in candidates if path.is_file()), candidates[0])
        codes: list[str] = []
        if codes_path.is_file():
            payload = json.loads(codes_path.read_text(encoding="utf-8"))
            plate_rows = payload.get("plates", []) if isinstance(payload, dict) else payload
            codes = [str(item.get("plate_code", item.get("code", ""))
                         if isinstance(item, dict) else item)
                     for item in plate_rows if isinstance(item, (dict, str, int))]
        return KaipanProvider(LazyKaipanClient(), plate_codes=codes, max_requests=max_requests)

    # Factories defer SDK construction and all requests until JobRunner invokes them.
    def pool_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        return KaipanProvider(LazyKaipanClient(), max_requests=max_requests).limit_up_pool(day, context=context)

    def hot_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        return KaipanProvider(LazyKaipanClient(), max_requests=max_requests).hot_plates(day, context=context)

    def members_fetch(day: str, context: Any = None) -> Any:
        provider = kaipan_provider(day)
        return provider.plate_members(day, context=context)

    def reasons_fetch(day: str, context: Any = None) -> Any:
        from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
        checkpoint = Path(artifact_root) / ".checkpoints" / f"limit_reasons-{day}.json" if write_enabled else None
        provider = KaipanProvider(LazyKaipanClient(), symbols=symbols_for(day), max_requests=max_requests,
                                  reasons_checkpoint_path=checkpoint)
        return provider.limit_reasons(day, context=context)

    return {"daily_kline": daily_fetch, "limit_up_pool": pool_fetch,
            "plate_members": members_fetch, "hot_plates": hot_fetch,
            "limit_reasons": reasons_fetch}


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
    parser.add_argument("--adapter", help="injected module:function returning dataset fetch callables")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--td-database", help="allowlisted independent database: core_data/core_intraday")
    args = parser.parse_args(argv)
    args.artifact_root = str(Path(args.artifact_root).expanduser())
    if args.td_write and (not args.write or not args.td_database):
        parser.error("--td-write requires --write and --td-database")
    names = list(DATASETS) if args.job == "all" else [args.job]
    adapters = _default_adapters(args.artifact_root,
        os.getenv("ENGINE_LEGACY_ROOT", "/home/exedev/repos/stock-situation-runtime"),
        args.max_requests, args.write)
    adapters.update(_load_adapter(args.adapter))
    missing = [name for name in names if name not in adapters]
    if missing: raise RuntimeError("fetcher_not_configured:" + ",".join(missing))
    redis = None
    if args.write:
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["CORE_REDIS_URL"], decode_responses=True)
    td_adapter = adapters.get("td_writer") if args.td_write else None
    runner = JobRunner(artifact_root=args.artifact_root, redis=redis, td_adapter=td_adapter,
                       budget=RequestBudget(max_requests=args.max_requests))
    for name in names:
        result = runner.run(name, args.date, fetch=adapters[name], publish=args.write,
                            td_write=args.td_write, source=get_dataset(name).source,
                            dry_run=not args.write)
        print(json.dumps({"job": name, "version": result.manifest.get("version"),
                          "row_count": result.manifest["row_count"],
                          "sha256": result.manifest.get("sha256"),
                          "quality": result.manifest["quality"],
                          "storage_uri": result.manifest["storage_uri"],
                          "cache_publish": result.manifest["cache_publish"],
                          "mode": result.manifest.get("mode", "WRITE")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
