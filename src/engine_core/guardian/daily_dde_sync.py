"""Guardian adapter for the Kaipan-backed daily_dde TD sink."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def run(request: Mapping[str, Any], root: Path, *, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    target_date = str(args.get("target_date") or request.get("data_date") or request.get("trade_date") or "")
    if not target_date:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "artifacts": [], "detail": "trade_date_required"}
    if dry_run:
        return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
                   "artifacts": [], "detail": {"dry_run": True, "target_date": target_date}}
    from ..data_pipeline.daily_dde import run_daily_dde_sync
    try:
        result = run_daily_dde_sync(
            args.get("symbols") if isinstance(args.get("symbols"), list) else None,
            target_date, database=str(args.get("database") or "") or None, root=root,
            max_workers=int(args.get("max_workers", 2)), attempts=int(args.get("attempts", 3)),
            history_trading_days=int(args.get("history_trading_days", 250)),
            redis_ttl_seconds=(int(args["redis_ttl_seconds"])
                               if args.get("redis_ttl_seconds") is not None else None),
            write_redis=bool(args.get("write_redis", True)), skip_redis=bool(args.get("skip_redis", False)))
    except Exception as exc:
        return 75, {"contract": 1, "status": "failed", "error_code": "DDE_SYNC_RETRYABLE",
                    "retryable": True, "artifacts": [],
                    "detail": {"error": type(exc).__name__ + ":" + str(exc)[:300], "target_date": target_date}}
    status = "ok" if result["status"] == "ok" else "failed"
    return (0 if status == "ok" else 75), {
        "contract": 1, "status": status, "error_code": "" if status == "ok" else "DDE_KNOWN_MISSING",
        "retryable": status != "ok", "artifacts": [], "detail": result}
