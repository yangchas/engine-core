#!/usr/bin/env python3
"""Thin adapter: build daily_indicators / daily_report_features from persisted daily_kline (C571).

Reads upstream partitions from the catalog, calls the pure builders in
``engine_core.data_pipeline.derived_daily`` and commits one ``source=derived_v1`` version. With ``--cascade``
(default) every later recent partition whose recorded inputs changed is rebuilt too, so an upgraded old day
propagates through the rolling windows in one run. Exit codes: 0 ok, 20 upstream not ready,
30 retryable (I/O), 40 deterministic (contract/argument).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from engine_core.data_pipeline.derived_daily import UpstreamNotReady  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402

DATASETS = ("daily_indicators", "daily_report_features")


def _later_days(catalog: ArtifactCatalog, day: str, limit: int) -> list[str]:
    days = sorted(d for d in catalog.read().get("datasets", {}).get("daily_kline", {}).get("dates", {}) if d > day)
    return days[:limit]


def _build(catalog: ArtifactCatalog, dataset: str, day: str, args, redis) -> dict:
    from engine_core import daily_history as dh
    if dataset == "daily_indicators":
        return dh.materialize_daily_indicators(catalog, trade_date=day, as_of=args.as_of,
                                               min_upstream_status=args.min_upstream_status, force=args.force,
                                               now=args.now_dt)
    result = dh.materialize_daily_report_features(catalog, trade_date=day, asof=args.as_of, redis=redis,
                                                  min_upstream_status=args.min_upstream_status, force=args.force,
                                                  now=args.now_dt)
    manifest = result.pop("manifest")
    result.update(version=manifest.get("version"), business_status=manifest.get("business_status"))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--as-of", help="tz-aware PIT cutoff (default: now)")
    parser.add_argument("--now", help="tz-aware fake clock for acceptance checks: PIT cutoff and commit clock")
    parser.add_argument("--min-upstream-status", default=None, choices=("PROVISIONAL", "FORMAL"))
    parser.add_argument("--no-cascade", dest="cascade", action="store_false")
    parser.add_argument("--cascade-days", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--publish-redis", action="store_true")
    args = parser.parse_args(argv)
    started = time.monotonic()
    args.now_dt = datetime.fromisoformat(args.now) if args.now else None
    if args.now_dt is not None and args.now_dt.tzinfo is None:
        parser.error("--now must be timezone-aware")
    if args.min_upstream_status is None:
        from engine_core.guardian.registry import load_registry
        item = next(i for i in load_registry() if i["id"] == args.dataset)
        args.min_upstream_status = (item.get("derive") or {}).get("min_upstream_status", "PROVISIONAL")
    catalog = ArtifactCatalog(Path(args.artifact_root).expanduser())
    redis = None
    if args.publish_redis:
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    out = {"job": args.dataset, "dataset": args.dataset, "trade_date": args.date}
    try:
        out.update(_build(catalog, args.dataset, args.date, args, redis))
        cascade = []
        if args.cascade:
            from engine_core.guardian.registry import derived_needs_build
            now = args.now_dt or datetime.now(timezone.utc)
            for later in _later_days(catalog, args.date, args.cascade_days):
                # a rebuilt day makes later days' recorded history versions stale; features also need indicators
                for dataset in DATASETS[DATASETS.index(args.dataset):]:
                    if derived_needs_build(catalog.read(), dataset, later, now, args.min_upstream_status):
                        if dataset == "daily_report_features" and args.dataset == "daily_indicators":
                            continue
                        cascade.append({"trade_date": later, **_build(catalog, dataset, later, args, redis)})
        out["cascade"] = cascade
        out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except UpstreamNotReady as exc:
        out.update(status="not_applicable", error_code=exc.code, error=str(exc))
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 20
    except (ValueError, KeyError, TypeError) as exc:
        out.update(status="failed", error_code="CONTRACT_ERROR", error=f"{type(exc).__name__}:{exc}"[:300])
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 40
    except (OSError, RuntimeError) as exc:
        out.update(status="failed", error_code="FETCH_TIMEOUT" if isinstance(exc, TimeoutError) else "IO_ERROR",
                   error=f"{type(exc).__name__}:{exc}"[:300])
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
