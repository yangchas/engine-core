"""Capture Kaipan hot-plate snapshots and plate membership into Core storage."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.data_pipeline.jobs import JobRunner, RequestBudget  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402
from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider  # noqa: E402


def _already_available(catalog: ArtifactCatalog, dataset: str, trade_date: str) -> dict | None:
    path = catalog.locate(dataset, trade_date)
    if path is None:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.get("row_count", 0) > 0 and manifest.get("quality", {}).get("status") != "FAILED":
        return manifest
    return None


def _registry_codes(catalog: ArtifactCatalog, trade_date: str) -> list[str]:
    path = catalog.locate("plate_registry", trade_date)
    if path is None:
        latest = catalog.locate("plate_registry")
        path = latest
    if path is None:
        raise RuntimeError("plate_registry_missing")
    return list(dict.fromkeys(str(row.get("plate_code") or row.get("code") or "")
                              for row in catalog.read_rows(path)
                              if row.get("plate_code") or row.get("code")))


def _kaipan_available_at(trade_date: str) -> str:
    available = date.fromisoformat(trade_date) + timedelta(days=1)
    return f"{available.isoformat()}T00:00:00+08:00"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", action="append", dest="trade_dates",
                        help="source trading date; repeat for historical backfill")
    parser.add_argument("--dataset", choices=("hot_plates", "plate_members", "hot_plate_minutes", "both"), default="both")
    parser.add_argument("--artifact-root", type=Path,
                        default=Path(os.environ.get("CORE_DATA_ROOT", Path.home() / "core-data")))
    parser.add_argument("--max-requests", type=int, default=40,
                        help="bounded provider requests per date (at least 1 second apart)")
    parser.add_argument("--minute-plates", type=int, default=30,
                        help="top N heat-ranked plates to capture minute trends")
    parser.add_argument("--execute", action="store_true", help="persist Core artifact and Redis hot view")
    args = parser.parse_args(argv)

    captured_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")
    trade_dates = args.trade_dates or [captured_at[:10]]
    client = None
    if args.execute:
        import redis
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            parser.error("REDIS_URL is required for --execute")
        client = redis.Redis.from_url(redis_url, socket_timeout=10, socket_connect_timeout=5)
    try:
        runner = JobRunner(artifact_root=args.artifact_root, redis=client,
                           budget=RequestBudget(max_requests=args.max_requests,
                                                min_interval_seconds=10))
        catalog = ArtifactCatalog(args.artifact_root)
        sdk = _KaipanSdk()
        datasets = (("hot_plates", "plate_members") if args.dataset == "both" else (args.dataset,))
        for dataset in datasets:
            dataset_dates = trade_dates if dataset in {"hot_plates", "hot_plate_minutes"} else [trade_dates[-1]]
            for trade_date in dataset_dates:
                existing = _already_available(catalog, dataset, trade_date)
                if existing and dataset == "hot_plates":
                    existing_manifest = catalog.locate(dataset, trade_date)
                    old_rows = catalog.read_rows(existing_manifest) if existing_manifest else []
                    required = (len(old_rows) == 50 and old_rows and
                                old_rows[0].get("requested_date") == trade_date and
                                old_rows[0].get("response_time") is not None and
                                old_rows[0].get("response_min") is not None and
                                old_rows[0].get("response_max") is not None and
                                old_rows[0].get("fetched_at") and old_rows[0].get("available_at"))
                    if not required:
                        existing = None
                if existing and dataset == "hot_plate_minutes":
                    existing_manifest = catalog.locate(dataset, trade_date)
                    old_rows = catalog.read_rows(existing_manifest) if existing_manifest else []
                    old_plates = {str(row.get("plate_code") or "") for row in old_rows}
                    counts = {code: sum(1 for row in old_rows if row.get("plate_code") == code)
                              for code in old_plates}
                    if (not old_rows or old_rows[0].get("minute_capture_limit") != max(1, args.minute_plates)
                            or len(old_plates) != max(1, args.minute_plates)
                            or any(count != 241 for count in counts.values())):
                        existing = None
                if existing:
                    print(f"SKIPPED_EXISTING dataset={dataset} date={trade_date} "
                          f"rows={existing.get('row_count')} version={existing.get('version')}")
                    continue
                date_mismatch = {"value": False}
                available_at = (_kaipan_available_at(trade_date)
                                if dataset == "hot_plate_minutes" else None)
                if dataset == "hot_plates":
                    provider = KaipanProvider(client=sdk, max_requests=args.max_requests)
                    def fetch(day, _ctx=None, provider=provider):
                        result = provider.hot_plates(day)
                        if not result["rows"]:
                            raise RuntimeError("kaipan_empty_hot_plates")
                        fields = {"requested_date": result["requested_date"],
                                  "response_time": result["response_time"],
                                  "response_min": result["response_min"],
                                  "response_max": result["response_max"],
                                  "response_day": result["response_day"],
                                  "fetched_at": result["fetched_at"],
                                  "available_at": result["available_at"]}
                        return {**result, "rows": [dict(row, **fields) for row in result["rows"]]}
                    source = "kaipan_getHisPlates"
                elif dataset == "hot_plate_minutes":
                    hot_path = catalog.locate("hot_plates", trade_date)
                    if hot_path is None:
                        raise RuntimeError(f"hot_plates_required_for_minutes:{trade_date}")
                    hot_rows = catalog.read_rows(hot_path)[:max(1, args.minute_plates)]
                    provider = KaipanProvider(client=sdk, max_requests=args.max_requests,
                                              min_interval_seconds=1)
                    def fetch(day, _ctx=None, provider=provider, hot_rows=hot_rows):
                        result_rows = []
                        for plate in hot_rows:
                            result = provider.plate_minutes(plate.get("plate_code", ""), day)
                            if len(result["rows"]) != 241:
                                raise RuntimeError(f"kaipan_empty_plate_minutes:{plate.get('plate_code')}")
                            metadata = result["metadata"]
                            result_rows.extend(dict(row, **metadata, requested_date=day,
                                minute_capture_limit=max(1, args.minute_plates),
                                fetched_at=result["fetched_at"], available_at=available_at)
                                for row in result["rows"])
                        return {"rows": result_rows, "expected_count": len(result_rows),
                                "request_count": provider._requests}
                    source = "kaipan_getHisPlateMins"
                else:
                    codes = _registry_codes(catalog, trade_date)
                    provider = KaipanProvider(client=sdk, plate_codes=codes,
                                              max_requests=args.max_requests)
                    def fetch(day, _ctx=None, provider=provider):
                        try:
                            result = provider.plate_members(day)
                        except ValueError as exc:
                            if str(exc) == "kaipan_trade_date_mismatch":
                                date_mismatch["value"] = True
                            raise
                        if not result["rows"]:
                            raise RuntimeError("kaipan_empty_plate_members")
                        return result
                    source = "kaipan_getHisPlateIds"

                try:
                    result = runner.run(dataset, trade_date, fetch=fetch, now=captured_at,
                    available_at=available_at if dataset == "hot_plate_minutes" else None,
                    as_of=trade_date, source=source,
                        publish=args.execute, dry_run=not args.execute,
                        compute=lambda rows: [dict(row, captured_at=row.get("captured_at", captured_at),
                            fetched_at=row.get("fetched_at", captured_at),
                            available_at=row.get("available_at", available_at or captured_at)) for row in rows])
                except RuntimeError:
                    if not date_mismatch["value"]:
                        raise
                    print(f"SKIPPED_DATE_MISMATCH dataset={dataset} requested={trade_date}")
                    continue
                if not result.rows:
                    raise RuntimeError(f"empty_result_not_published:{dataset}:{trade_date}")
                print(f"CAPTURED dataset={dataset} date={trade_date} rows={len(result.rows)} "
                      f"status={result.manifest.get('business_status', result.manifest['quality']['status'])} "
                      f"source={result.manifest['source']} version={result.manifest.get('version', 'DRY_RUN')} "
                      f"cache={result.manifest.get('cache_publish', 'SKIPPED')}")
        return 0
    finally:
        if client is not None:
            client.close()


class _KaipanSdk:
    """Resolve the separately installed SDK lazily so dry-run imports stay simple."""

    def __init__(self):
        import requests
        post = requests.post

        def bounded_post(*args, **kwargs):
            kwargs.setdefault("timeout", 10)
            return post(*args, **kwargs)

        requests.post = bounded_post

    def __getattr__(self, name):
        import pykaipan.pykaipan as sdk
        return getattr(sdk, {"get_his_plates": "getHisPlates",
                             "get_his_plate_ids": "getHisPlateIds"}.get(name, name))


if __name__ == "__main__":
    raise SystemExit(main())
