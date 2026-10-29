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
from engine_core.data_pipeline.hot_plates_builder import build_hot_plates  # noqa: E402
from engine_core.data_pipeline.hot_plates_gaps import (  # noqa: E402
    reconcile_due_hot_plates_gaps, record_hot_plates_gap,
)


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


def _historical_gap_window(trade_date: str, trading_dates: list[str]) -> tuple[str, str, str]:
    next_session = next((day for day in trading_dates if day > trade_date), None)
    if next_session is None:
        raise RuntimeError("hot_plates_calendar_history_unknown")
    event_id = f"kaipan_history_{trade_date.replace('-', '')}"
    return (event_id, f"{next_session}T08:50:00+08:00",
            f"{next_session}T09:05:00+08:00")


def main(argv=None, *, now_fn=None) -> int:
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
    parser.add_argument("--max-pages", type=int, default=5,
                        help="maximum Kaipan hot-plate pages for one snapshot")
    parser.add_argument("--event-id", help="schedule event represented by this hot snapshot")
    parser.add_argument("--scheduled-at", help="schedule start time HH:MM:SS Asia/Shanghai")
    parser.add_argument("--deadline", help="schedule deadline HH:MM:SS Asia/Shanghai")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="record expired hot-plate event gaps without calling Kaipan")
    parser.add_argument("--execute", action="store_true", help="persist Core artifact and Redis hot view")
    parser.add_argument("--write-artifact", action="store_true",
                        help="persist only the immutable Core artifact (no Redis write)")
    args = parser.parse_args(argv)

    now_fn = now_fn or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
    clock = now_fn()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    captured_at = clock.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")
    trade_dates = args.trade_dates or [captured_at[:10]]
    persist = args.execute or args.write_artifact
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
                                                min_interval_seconds=1))
        catalog = ArtifactCatalog(args.artifact_root)
        if args.reconcile_only:
            gaps = reconcile_due_hot_plates_gaps(args.artifact_root, captured_at[:10],
                                                  captured_at, persist=persist)
            print(f"RECONCILED dataset=hot_plates gaps={len(gaps)} "
                  f"late_policy=gap_only provider_requests=0")
            return 0
        if args.dataset == "hot_plates" and args.event_id and args.scheduled_at and args.deadline:
            scheduled = datetime.fromisoformat(f"{trade_dates[0]}T{args.scheduled_at}+08:00")
            deadline = datetime.fromisoformat(f"{trade_dates[0]}T{args.deadline}+08:00")
            if clock.astimezone(ZoneInfo("Asia/Shanghai")) > deadline:
                gap = record_hot_plates_gap(args.artifact_root, trade_dates[0], args.event_id,
                    scheduled.isoformat(), deadline.isoformat(), captured_at,
                    "late_startup_after_deadline", persist=persist)
                print(f"GAP dataset=hot_plates event={gap['event_id']} reason={gap['gap_reason']} "
                      f"late_policy={gap['late_policy']} fallback={gap['fallback_status']}")
                for prior in reconcile_due_hot_plates_gaps(args.artifact_root, trade_dates[0],
                        captured_at, persist=persist):
                    if prior["event_id"] != gap["event_id"]:
                        print(f"GAP dataset=hot_plates event={prior['event_id']} "
                              f"reason={prior['gap_reason']} late_policy={prior['late_policy']} "
                              f"fallback={prior['fallback_status']}")
                return 0
        if args.dataset == "hot_plates" and args.event_id:
            for gap in reconcile_due_hot_plates_gaps(args.artifact_root, trade_dates[0],
                    captured_at, persist=persist):
                print(f"GAP dataset=hot_plates event={gap['event_id']} reason={gap['gap_reason']} "
                      f"late_policy={gap['late_policy']} fallback={gap['fallback_status']}")
        historical_sessions: list[str] = []
        retained_historical_dates: set[str] = set()
        if any(day < captured_at[:10] for day in trade_dates):
            from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
            calendar_root = os.environ.get("CORE_DATA_ROOT", args.artifact_root)
            historical_sessions = TradeCalendarStore(calendar_root).all_trading_dates()
            if not historical_sessions:
                raise RuntimeError("hot_plates_calendar_history_unknown")
            retained_historical_dates = {
                day for day in historical_sessions if day < captured_at[:10]
            }
            retained_historical_dates = set(sorted(retained_historical_dates)[-3:])
        sdk = _KaipanSdk()
        datasets = (("hot_plates", "plate_members") if args.dataset == "both" else (args.dataset,))
        for dataset in datasets:
            dataset_dates = trade_dates if dataset in {"hot_plates", "hot_plate_minutes"} else [trade_dates[-1]]
            for trade_date in dataset_dates:
                if (dataset == "hot_plates" and trade_date < captured_at[:10]
                        and trade_date not in retained_historical_dates):
                    event_id, scheduled_at, deadline = _historical_gap_window(
                        trade_date, historical_sessions)
                    gap = record_hot_plates_gap(args.artifact_root, trade_date, event_id,
                        scheduled_at, deadline, captured_at, "historical_outside_retention",
                        persist=persist)
                    print(f"GAP dataset=hot_plates date={trade_date} event={gap['event_id']} "
                          f"reason={gap['gap_reason']} late_policy={gap['late_policy']} "
                          f"fallback={gap['fallback_status']} requests=0")
                    continue
                capture_event_id = args.event_id
                if dataset == "hot_plates" and not capture_event_id:
                    if trade_date < captured_at[:10]:
                        capture_event_id, _, _ = _historical_gap_window(
                            trade_date, historical_sessions)
                    else:
                        timestamp_tag = (captured_at.replace("-", "").replace(":", "")
                                         .replace("+", "").replace(".", ""))
                        capture_event_id = f"kaipan_hot_manual_{timestamp_tag}"
                existing = None if dataset == "hot_plates" else _already_available(catalog, dataset, trade_date)
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
                    provider = KaipanProvider(client=sdk, max_requests=args.max_requests,
                                              min_interval_seconds=1)
                    def fetch(day, _ctx=None, provider=provider):
                        request_date = "" if day == captured_at[:10] else day
                        result = provider.hot_plates(day, request_date=request_date,
                                                     max_pages=args.max_pages)
                        if not result["rows"]:
                            raise RuntimeError("kaipan_empty_hot_plates")
                        built = build_hot_plates({"trade_date": day,
                            "captured_at": result["fetched_at"],
                            "response": result["raw_response"], "page_size": 50,
                            "pages_received": result["pages_received"],
                            "requested_api_date": result["requested_api_date"],
                            "event_id": capture_event_id,
                            "scheduled_at": (f"{day}T{args.scheduled_at}+08:00"
                                             if args.scheduled_at else None),
                            "deadline": (f"{day}T{args.deadline}+08:00"
                                         if args.deadline else None),
                            "provider_attempt": result["provider_attempt"]})
                        return {**built.manifest, "rows": built.rows,
                                "request_count": result["request_count"]}
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
                    compute_rows = (None if dataset == "hot_plates" else
                        lambda rows: [dict(row, captured_at=row.get("captured_at", captured_at),
                            fetched_at=row.get("fetched_at", captured_at),
                            available_at=row.get("available_at", available_at or captured_at)) for row in rows])
                    result = runner.run(dataset, trade_date, fetch=fetch, now=captured_at,
                    available_at=available_at if dataset == "hot_plate_minutes" else None,
                    as_of=trade_date, source=source,
                        publish=args.execute, dry_run=not persist,
                        compute=compute_rows)
                except Exception as exc:
                    error = str(exc).lower()
                    date_mismatch["value"] = date_mismatch["value"] or (
                        dataset == "hot_plates" and
                        ("date_mismatch" in error or "response_date" in error))
                    if dataset == "hot_plates" and args.event_id and args.scheduled_at and args.deadline:
                        scheduled = datetime.fromisoformat(f"{trade_date}T{args.scheduled_at}+08:00")
                        deadline = datetime.fromisoformat(f"{trade_date}T{args.deadline}+08:00")
                        reason = "response_day_mismatch" if date_mismatch["value"] else "provider_failure"
                        now_local = clock.astimezone(ZoneInfo("Asia/Shanghai"))
                        if date_mismatch["value"] or now_local >= deadline:
                            gap = record_hot_plates_gap(args.artifact_root, trade_date, args.event_id,
                                scheduled.isoformat(), deadline.isoformat(), captured_at,
                                reason, persist=persist)
                            print(f"GAP dataset=hot_plates event={gap['event_id']} reason={gap['gap_reason']} "
                                  f"late_policy={gap['late_policy']} fallback={gap['fallback_status']}")
                            continue
                        raise RuntimeError(f"hot_plates_retryable_provider_failure:{type(exc).__name__}") from None
                    if dataset == "hot_plates" and trade_date < captured_at[:10]:
                        event_id, scheduled_at, deadline = _historical_gap_window(
                            trade_date, historical_sessions)
                        reason = ("response_day_mismatch" if date_mismatch["value"]
                                  else "historical_provider_failure")
                        gap = record_hot_plates_gap(args.artifact_root, trade_date, event_id,
                            scheduled_at, deadline, captured_at, reason, persist=persist)
                        print(f"GAP dataset=hot_plates date={trade_date} event={gap['event_id']} "
                              f"reason={gap['gap_reason']} late_policy={gap['late_policy']} "
                              f"fallback={gap['fallback_status']}")
                        continue
                    if date_mismatch["value"]:
                        print(f"SKIPPED_DATE_MISMATCH dataset={dataset} requested={trade_date}")
                        continue
                    raise
                if not result.rows:
                    raise RuntimeError(f"empty_result_not_published:{dataset}:{trade_date}")
                print(f"CAPTURED dataset={dataset} date={trade_date} rows={len(result.rows)} "
                      f"event={result.manifest.get('event_id', '')} "
                      f"status={result.manifest.get('business_status', result.manifest['quality']['status'])} "
                      f"source={result.manifest['source']} version={result.manifest.get('version', 'DRY_RUN')} "
                      f"requests={result.manifest.get('request_count', 0)} "
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
