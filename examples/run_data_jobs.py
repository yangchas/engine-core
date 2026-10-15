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


def _load_adapter(spec: str | None) -> dict[str, Any]:
    if not spec: return {}
    module_name, separator, factory_name = spec.partition(":")
    if not separator: raise ValueError("adapter must be module:function")
    factory = getattr(importlib.import_module(module_name), factory_name)
    value = factory()
    if not isinstance(value, dict): raise ValueError("adapter factory must return a mapping")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, choices=["all", *DATASETS.keys()])
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--write", action="store_true", help="publish bounded views to Core Redis")
    parser.add_argument("--td-write", action="store_true", help="write through an injected independent TD adapter")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", help="run fetch and commit the artifact version")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--adapter", help="injected module:function returning dataset fetch callables")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "/home/exedev/validation/core-data"))
    parser.add_argument("--td-database", help="allowlisted independent database: core_data/core_intraday")
    args = parser.parse_args(argv)
    if args.write and not args.execute: parser.error("--write requires --execute")
    if args.td_write and (not args.execute or not args.td_database):
        parser.error("--td-write requires --execute and --td-database")
    names = list(DATASETS) if args.job == "all" else [args.job]
    if not args.execute:
        for name in names:
            spec = get_dataset(name)
            print(json.dumps({"job": name, "date": args.date, "source": spec.source,
                              "schedule": spec.schedule, "primary_backend": spec.primary_backend,
                              "redis_ttl_seconds": spec.redis_ttl_seconds,
                              "mode": "DRY_RUN_PLAN"}, sort_keys=True))
        return 0
    adapters = _load_adapter(args.adapter)
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
                            td_write=args.td_write, source=get_dataset(name).source)
        print(json.dumps({"job": name, "version": result.manifest["version"],
                          "row_count": result.manifest["row_count"],
                          "sha256": result.manifest["sha256"],
                          "quality": result.manifest["quality"],
                          "storage_uri": result.manifest["storage_uri"],
                          "cache_publish": result.manifest["cache_publish"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
