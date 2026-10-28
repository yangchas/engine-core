#!/usr/bin/env python3
"""Thin adapter: build ladder_daily for one trade date (C577, docs/LADDER_DAILY.md).

Reads the configuration from the registry item ``ladder_daily.derive`` (rung order, window, live rung limits),
loads inputs through ``engine_core.ladder_daily_adapter``, commits one version, then (best effort, TD configured)
copies it to Core TD. Exit codes: 0 ok, 20 upstream/limit-up set not available, 30 retryable, 40 deterministic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from engine_core.data_pipeline.derived_daily import UpstreamNotReady  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402


def registry_config() -> dict:
    from engine_core.guardian.registry import load_registry
    item = next(i for i in load_registry() if i["id"] == "ladder_daily")
    derive = dict(item.get("derive") or {})
    return {k: derive[k] for k in ("source_ladder", "continuity_min_ratio", "kaipan", "wencai") if k in derive} | {
        "window_days": derive.get("window_days", 20)}


def td_write(root: Path, day: str, rows, manifest) -> dict:
    """Write and verify this task's in-memory partition before reporting success."""
    from engine_core.data_pipeline.td_ladder_sink import LadderTdSink, TaosLadderStore
    from engine_core.data_pipeline.td_sink import TdUnavailable, TdVerifyError
    from engine_core.guardian.daily_kline_coverage import td_settings
    settings = td_settings()
    if settings is None or not os.environ.get("TD_CONFIG"):
        raise TdUnavailable("td_sink_not_configured")
    store = TaosLadderStore(str(settings["database"]))
    try:
        outcome = LadderTdSink(store, root).write_partition(day, rows, manifest)
    finally:
        store.close()
    if outcome.status not in {"synced", "unchanged", "skipped_lower"}:
        raise TdVerifyError("td_ladder_write_failed:" + day + ":" + outcome.status)
    return {"status": "verified", "database": settings["database"], "outcome": outcome.as_dict()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--now", help="tz-aware fake clock (acceptance checks): PIT cutoff and commit clock")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    started = time.monotonic()
    now = datetime.fromisoformat(args.now) if args.now else None
    if now is not None and now.tzinfo is None:
        parser.error("--now must be timezone-aware")
    root = Path(args.artifact_root).expanduser()
    out = {"job": "ladder_daily", "dataset": "ladder_daily", "trade_date": args.date}
    try:
        from engine_core.ladder_daily_adapter import materialize_ladder_daily
        out.update(materialize_ladder_daily(
            ArtifactCatalog(root), trade_date=args.date, config=registry_config(), now=now, force=args.force,
            state_dir=root / "guardian-native", on_committed=lambda day, rows, manifest:
            td_write(root, day, rows, manifest)))
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
    except Exception as exc:
        from engine_core.data_pipeline.td_sink import TdUnavailable, TdVerifyError
        if isinstance(exc, TdUnavailable):
            code, error_code = 30, "TD_UNAVAILABLE"
        elif isinstance(exc, TdVerifyError):
            code, error_code = 40, "TD_VERIFY_FAILED"
        else:
            code, error_code = 30, "FETCH_TIMEOUT" if isinstance(exc, TimeoutError) else "IO_ERROR"
        out.update(status="failed", error_code=error_code, error=f"{type(exc).__name__}:{exc}"[:300])
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return code


if __name__ == "__main__":
    raise SystemExit(main())
